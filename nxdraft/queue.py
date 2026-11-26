"""JSON 命令队列：给别的程序调用的入口。

协议沿用 dsh-nx 的形状，方便已有代码迁移：

    req/<id>.json   {"id": "abc12345", "cmd": "drawing", "args": {...}}
    resp/<id>.json  {"id": "abc12345", "ok": true, "result": {...}}
                    {"id": "abc12345", "ok": false, "error": "...", "trace": "..."}

每个请求在**独立的 NX batch 会话**里跑（NX 一个进程只能有一个会话，且退出后
状态不保留），所以队列是串行处理的：一次一个请求，处理完再取下一个。

`cmd` 是 nxdraft.api 里的函数名（drawing / dimension / pdf / batch_drawings /
bom / bom_table / section_view ...）。`args` 是该函数的具名参数。

如果 args 里带 `"path"`，会先打开那个 .prt 再执行命令。
"""

import json
import os
import sys
import time
import traceback
import uuid

from . import api, launcher


def _write(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, default=str)
    os.replace(temporary, path)


def handle_one(nx_home, out_dir, request, timeout=1800):
    """处理一个请求，返回响应 dict。异常都收进响应里，不让队列挂掉。"""
    rid = request.get("id") or uuid.uuid4().hex[:8]
    command = request.get("cmd")
    args = dict(request.get("args") or {})

    started = time.time()
    try:
        handler = getattr(api, command, None)
        if handler is None:
            raise RuntimeError("unknown command: %s" % command)

        # 需要先打开的部件
        open_paths = []
        explicit = args.pop("path", None)
        if explicit:
            open_paths.append(explicit)
        elif command in ("batch_drawings", "bom", "bom_table") and args.get("assembly"):
            open_paths.append(args.pop("assembly"))

        # 队列模式下不能直接调 api.xxx（那需要活的 NX 会话），
        # 而是交给 launcher 在独立 NX 进程里跑。
        result, logs = launcher.run_command(
            nx_home, command, args, out_dir, timeout=timeout, open_paths=open_paths)
        return {
            "id": rid,
            "ok": True,
            "result": result,
            "logs": logs,
            "ms": int((time.time() - started) * 1000),
        }
    except Exception as error:
        return {
            "id": rid,
            "ok": False,
            "error": "%s: %s" % (type(error).__name__, error),
            "trace": traceback.format_exc(),
            "ms": int((time.time() - started) * 1000),
        }


def serve(req_dir, resp_dir, out_dir, nx_home, poll=0.05, timeout=1800, verbose=True):
    """常驻循环：处理 req/ 里的请求，结果写进 resp/。Ctrl+C 退出。"""
    for directory in (req_dir, resp_dir, out_dir):
        if not os.path.isdir(directory):
            os.makedirs(directory)

    if verbose:
        print("nxdraft queue: %s -> %s" % (req_dir, resp_dir))
        print("  NX home : %s" % nx_home)
        print("  产物目录: %s" % out_dir)
        print("  等待请求…（Ctrl+C 退出）")

    idle = 0.0
    try:
        while True:
            pending = sorted(name for name in os.listdir(req_dir) if name.endswith(".json"))
            if not pending:
                idle = min(0.5, (idle * 1.6) if idle else poll)
                time.sleep(idle)
                continue
            idle = 0.0
            for name in pending:
                path = os.path.join(req_dir, name)
                try:
                    with open(path, "r", encoding="utf-8-sig") as handle:
                        request = json.load(handle)
                except Exception as error:
                    if verbose:
                        print("跳过无法解析的请求 %s: %s" % (name, error))
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    continue

                rid = request.get("id") or os.path.splitext(name)[0]
                if verbose:
                    print("-> %s %s" % (rid, request.get("cmd")))
                response = handle_one(nx_home, out_dir, request, timeout=timeout)
                _write(os.path.join(resp_dir, rid + ".json"), response)
                if verbose:
                    status = "ok" if response.get("ok") else "失败"
                    print("<- %s %s (%s ms)" % (rid, status, response.get("ms")))
                try:
                    os.remove(path)
                except Exception:
                    pass
    except KeyboardInterrupt:
        if verbose:
            print("\n已停止")
        return 0
