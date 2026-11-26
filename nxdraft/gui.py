"""本地 Web UI 的后端：一个只监听 127.0.0.1 的小 HTTP 服务。

界面是单个 HTML 页面（web/index.html），通过 fetch 调这里的 JSON 接口。
所有耗时操作（出图、批量、BOM）都放到后台线程，前端轮询 /api/job/<id> 拿进度，
否则浏览器会卡在「等待响应」。

安全：只绑 127.0.0.1；每个进程生成一次性 token，页面加载时注入，
接口校验 token 与 Origin —— 避免本机其他网页跨站调我们的接口去驱动 NX。
"""

import json
import os
import subprocess
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import launcher, options

HERE = os.path.dirname(os.path.abspath(__file__))


def _web_dir():
    """前端页面目录。exe 打包后文件被释放到 sys._MEIPASS 下。"""
    import sys
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = os.path.join(meipass, "nxdraft", "web")
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(HERE, "web")


WEB_DIR = _web_dir()


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class Job(object):
    """一个后台任务：状态、日志、结果。"""

    def __init__(self, job_id, title):
        self.id = job_id
        self.title = title
        self.state = "running"      # running | done | failed
        self.started = time.time()
        self.finished = None
        self.result = None
        self.error = None
        self.trace = None
        self.logs = []

    def log(self, text):
        self.logs.append(text)

    def snapshot(self):
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "result": self.result,
            "error": self.error,
            "trace": self.trace,
            "logs": self.logs[-200:],
        }


class Server(object):
    def __init__(self, nx_home, out_dir, host="127.0.0.1", port=0):
        self.nx_home = nx_home
        self.out_dir = out_dir
        self.host = host
        self.port = port
        self.token = uuid.uuid4().hex
        self.jobs = {}
        self.lock = threading.Lock()
        self.httpd = None

    # ---- 任务 ----

    def start_job(self, title, func):
        job = Job(uuid.uuid4().hex[:12], title)
        with self.lock:
            self.jobs[job.id] = job

        def run():
            try:
                job.result = func(job)
                job.state = "done"
            except Exception as error:
                job.state = "failed"
                job.error = "%s: %s" % (type(error).__name__, error)
                job.trace = traceback.format_exc()
            finally:
                job.finished = time.time()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return job

    def get_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        return job.snapshot() if job else None

    # ---- 业务操作 ----

    def run_nx(self, command, params, job, open_paths=None):
        """在 NX 里跑一个 api 函数。"""
        results = self.run_nx_steps([(command, params)], job, open_paths)
        return results.get(command)

    def run_nx_steps(self, steps, job, open_paths=None):
        """在**同一个** NX 会话里按顺序跑多个 api 函数。

        出图 → 标注 → 打印必须一次跑完：NX 进程退出后图纸就不在内存里了，
        分开调用会报 "the part has no drafting views yet"。
        """
        return launcher.run_steps(
            self.nx_home, steps, self.out_dir,
            open_paths=open_paths, timeout=3600,
            log_callback=job.log if job else None)

    def serve_forever(self):
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self.httpd.server_address[1]
        return self.httpd

    def _make_handler(server_self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass    # 不刷屏

            # ---- 工具 ----

            def _send(self, status, payload, content_type="application/json; charset=utf-8"):
                body = payload if isinstance(payload, bytes) else _json_bytes(payload)
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.send_header("cache-control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def _origin_ok(self):
                """只接受本机来源，防跨站调用。"""
                origin = self.headers.get("origin") or ""
                if not origin:
                    return True     # 同页 fetch 不带 Origin 的也放行
                try:
                    host = urlparse(origin).hostname or ""
                except Exception:
                    return False
                return host in ("127.0.0.1", "localhost", "::1")

            def _token_ok(self, token):
                return token and token == server_self.token

            def _read_json(self):
                length = int(self.headers.get("content-length") or 0)
                if not length:
                    return {}
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8"))

            # ---- 路由 ----

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if path in ("/", "/index.html"):
                    return self._serve_index()
                if path == "/api/info":
                    return self._send(200, {
                        "nxHome": server_self.nx_home,
                        "outDir": server_self.out_dir,
                        "version": _version(),
                    })
                if path.startswith("/api/job/"):
                    job_id = path.rsplit("/", 1)[-1]
                    snap = server_self.get_job(job_id)
                    if snap is None:
                        return self._send(404, {"error": "no such job"})
                    return self._send(200, snap)
                if path == "/api/jobs":
                    with server_self.lock:
                        items = [j.snapshot() for j in server_self.jobs.values()]
                    items.sort(key=lambda x: x["started"], reverse=True)
                    return self._send(200, {"jobs": items[:30]})
                if path == "/api/files":
                    return self._send(200, {"files": _recent_files(server_self.out_dir)})
                if path == "/api/part":
                    return self._send(200, _inspect_part(query.get("path", [""])[0]))
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._origin_ok():
                    return self._send(403, {"error": "cross-origin request refused"})
                path = urlparse(self.path).path
                try:
                    body = self._read_json()
                except Exception as error:
                    return self._send(400, {"error": "bad json: %s" % error})

                if not self._token_ok(body.get("token")):
                    return self._send(403, {"error": "bad token"})

                # 页面统一往 /api/action 投，body.action 决定做什么
                action = body.get("action")
                try:
                    return self._dispatch_post(action, body)
                except Exception as error:
                    return self._send(200, {
                        "ok": False,
                        "error": "%s: %s" % (type(error).__name__, error),
                        "trace": traceback.format_exc(),
                    })

            def _dispatch_post(self, action, body):
                if action == "drawing":
                    part = body.get("path") or ""
                    if not part:
                        return self._send(400, {"error": "path required"})
                    size = body.get("size") or "A3"
                    make_pdf = body.get("pdf", True)
                    # views 现在是列表（中文或英文都行），后端归一化成 flags
                    views, unknown = options.views_to_flags(body.get("views") or [])
                    if unknown:
                        return self._send(400, {
                            "error": "认不出的视图名: %s（可用：%s）" % (
                                "、".join(unknown),
                                "、".join(label for _k, label, _n in options.VIEW_NAMES))})
                    material = body.get("material") or None
                    drawing_no = body.get("drawingNo") or None
                    want_length = body.get("length", True) is not False
                    want_diameter = body.get("diameter", True) is not False

                    def work(job):
                        job.log("出图: %s (%s)" % (part, size))
                        if views:
                            job.log("指定视图: %s" % "、".join(
                                options.view_label(k) for k in views))
                        params = {"size": size}
                        if views:
                            params["views"] = views
                        if material:
                            params["material"] = material
                        if drawing_no:
                            params["drawing_no"] = drawing_no

                        # 一次会话里跑完：出图 → 标注 → PDF。
                        # 分成三次调用会失败，因为 NX 进程退出后图纸就没了。
                        dim_params = {"length": want_length, "diameter": want_diameter}
                        steps = [("drawing", params), ("dimension", dim_params)]
                        if make_pdf:
                            pdf_path = os.path.join(
                                server_self.out_dir,
                                os.path.splitext(os.path.basename(part))[0] + ".pdf")
                            steps.append(("pdf", {"path": pdf_path}))

                        results = server_self.run_nx_steps(steps, job, [part])
                        drawing = results.get("drawing") or {}
                        dims = results.get("dimension") or {}
                        out = {"drawing": drawing, "dimension": dims}
                        if "pdf" in results:
                            out["pdf"] = results["pdf"]
                        return out

                    job = server_self.start_job("出图 %s" % os.path.basename(part), work)
                    return self._send(200, {"ok": True, "job": job.id})

                if action == "analyze":
                    assembly = body.get("path") or ""
                    if not assembly:
                        return self._send(400, {"error": "path required"})

                    def work(job):
                        job.log("分析装配: %s" % assembly)
                        return server_self.run_nx("assembly_overview",
                                                  {"path": assembly}, job, [assembly])

                    job = server_self.start_job("分析 %s" % os.path.basename(assembly), work)
                    return self._send(200, {"ok": True, "job": job.id})

                if action == "batch":
                    assembly = body.get("path") or ""
                    size = body.get("size") or "A4"
                    if not assembly:
                        return self._send(400, {"error": "path required"})
                    order = [str(name) for name in (body.get("order") or []) if str(name).strip()]
                    prefix = body.get("numberPrefix") or ""
                    start = int(body.get("numberStart") or 1)
                    digits = int(body.get("numberDigits") or 0)
                    material = body.get("material") or None
                    want_length = body.get("length", True) is not False
                    want_diameter = body.get("diameter", True) is not False
                    want_asm = body.get("assemblyDrawing", True) is not False

                    def work(job):
                        job.log("批量出图: %s (%s)" % (assembly, size))
                        if order:
                            job.log("图纸顺序: %s" % " → ".join(order))
                        if prefix or start != 1 or digits:
                            job.log("图号规则: 前缀=%r 起始=%d 补零=%d" % (prefix, start, digits))
                        # 参数名必须与 api.batch_drawings 的形参一致（下划线风格）
                        params = {
                            "size": size,
                            "reuse": False,
                            "length": want_length,
                            "diameter": want_diameter,
                            "number_prefix": prefix,
                            "number_start": start,
                            "number_digits": digits,
                            "assembly_drawing": want_asm,
                        }
                        if order:
                            params["order"] = order
                        if material:
                            params["material"] = material
                        return server_self.run_nx("batch_drawings", params, job, [assembly])

                    job = server_self.start_job("批量 %s" % os.path.basename(assembly), work)
                    return self._send(200, {"ok": True, "job": job.id})

                if action == "bom":
                    assembly = body.get("path") or ""
                    if not assembly:
                        return self._send(400, {"error": "path required"})

                    def work(job):
                        job.log("明细表: %s" % assembly)
                        return server_self.run_nx("bom", {}, job, [assembly])

                    job = server_self.start_job("明细表 %s" % os.path.basename(assembly), work)
                    return self._send(200, {"ok": True, "job": job.id})

                if action == "open":
                    target = body.get("path") or ""
                    if target and os.path.exists(target):
                        # 文件就用资源管理器选中它，目录就直接打开
                        if os.path.isdir(target):
                            os.startfile(target)
                        else:
                            subprocess.Popen(["explorer.exe", "/select," + target])
                        return self._send(200, {"ok": True})
                    return self._send(404, {"error": "no such file"})

                if action == "open-out":
                    if os.path.isdir(server_self.out_dir):
                        os.startfile(server_self.out_dir)
                        return self._send(200, {"ok": True})
                    return self._send(404, {"error": "output dir missing"})

                if action == "set-out-dir":
                    target = str(body.get("path") or "").strip().strip('"')
                    if not target:
                        return self._send(400, {"error": "path required"})
                    # 允许相对路径（相对当前产物目录），绝对路径直接用
                    if not os.path.isabs(target):
                        target = os.path.join(server_self.out_dir, target)
                    target = os.path.abspath(target)
                    try:
                        if not os.path.isdir(target):
                            os.makedirs(target)
                        probe = os.path.join(target, ".nxdraft-write-test")
                        with open(probe, "w", encoding="utf-8") as handle:
                            handle.write("ok")
                        os.remove(probe)
                    except Exception as error:
                        return self._send(400, {
                            "error": "目录不可写: %s (%s)" % (target, error)})
                    server_self.out_dir = target
                    return self._send(200, {"ok": True, "outDir": target})

                return self._send(400, {"error": "unknown action: %s" % action})

            # ---- 静态页面 ----

            def _serve_index(self):
                page = os.path.join(_web_dir(), "index.html")
                try:
                    with open(page, "r", encoding="utf-8") as handle:
                        html = handle.read()
                except Exception as error:
                    return self._send(500, {"error": "index.html missing: %s (%s)" % (page, error)})
                # 把 token 和运行信息注入页面，前端不用再问
                bootstrap = {
                    "token": server_self.token,
                    "nxHome": server_self.nx_home,
                    "outDir": server_self.out_dir,
                }
                html = html.replace(
                    "/*__BOOTSTRAP__*/",
                    "window.BOOTSTRAP = %s;" % json.dumps(bootstrap, ensure_ascii=False))
                return self._send(200, html.encode("utf-8"),
                                  "text/html; charset=utf-8")

        return Handler


def _version():
    from . import __version__
    return __version__


def _recent_files(out_dir, limit=40):
    """产物目录里最近的 PDF / PRT / CSV，给界面列出来。"""
    found = []
    if not os.path.isdir(out_dir):
        return found
    for name in os.listdir(out_dir):
        lower = name.lower()
        if not lower.endswith((".pdf", ".prt", ".csv", ".png")):
            continue
        full = os.path.join(out_dir, name)
        try:
            info = os.stat(full)
        except Exception:
            continue
        found.append({
            "name": name,
            "path": full,
            "size": info.st_size,
            "mtime": info.st_mtime,
            "kind": lower.rsplit(".", 1)[-1],
        })
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found[:limit]


def _inspect_part(path):
    """不启 NX，只看文件本身的信息。"""
    if not path or not os.path.exists(path):
        return {"exists": False}
    info = os.stat(path)
    return {
        "exists": True,
        "name": os.path.basename(path),
        "path": os.path.abspath(path),
        "size": info.st_size,
        "mtime": info.st_mtime,
    }


def run(nx_home, out_dir, host="127.0.0.1", port=0, open_browser=True):
    """起服务并打开浏览器。阻塞直到 Ctrl+C。"""
    server = Server(nx_home, out_dir, host=host, port=port)
    httpd = server.serve_forever()
    url = "http://%s:%d/" % (host, server.port)
    print("nxdraft GUI: %s" % url)
    print("  NX home : %s" % nx_home)
    print("  产物目录: %s" % out_dir)
    print("  Ctrl+C 退出")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    return 0
