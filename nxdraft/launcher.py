"""起一个 NX batch 会话并在里面跑我们的代码。

NX 的 Python 不是普通 Python：NXOpen 只在 `run_journal.exe` 拉起的进程里存在。
所以独立程序的结构是：

    nxdraft CLI (普通 Python)
        └─ 写一个 journal 脚本到临时目录
           └─ run_journal.exe <journal>      ← NX 自己起进程、自己注入 Python
              └─ journal 里 import nxdraft，跑真正的制图逻辑

`run_journal.exe` 会把 stdout 交回来，所以我们用约定的前缀行把结果 JSON 传出去。

NX 的定位顺序：--nx-home 参数 → UGII_BASE_DIR 环境变量 → 常见安装目录探测。
"""

import json
import os
import subprocess
import sys
import tempfile
import time

# 常见安装位置，顺序即优先级
NX_ROOTS = [
    r"C:\Program Files\Siemens",
    r"C:\Program Files\Siemens PLM Software",
    r"C:\Program Files (x86)\Siemens",
    r"C:\Siemens",
    r"C:\software",
    r"C:\apps",
    r"D:\Program Files\Siemens",
    r"D:\Program Files\Siemens PLM Software",
    r"D:\Siemens",
    r"D:\software",
    r"D:\apps",
]

RESULT_PREFIX = "@@NXDRAFT_RESULT@@ "
LOG_PREFIX = "@@NXDRAFT_LOG@@ "


def is_nx_home(path):
    """这个目录像不像 NX 安装根（有 NXBIN\\run_journal.exe）。"""
    if not path:
        return False
    try:
        return os.path.isfile(os.path.join(path, "NXBIN", "run_journal.exe"))
    except Exception:
        return False


def find_nx_home(explicit=None):
    """找一个可用的 NX 安装根。找不到抛异常。"""
    if explicit:
        if not is_nx_home(explicit):
            raise RuntimeError("%s is not an NX install (no NXBIN\\run_journal.exe)" % explicit)
        return explicit

    env = os.environ.get("UGII_BASE_DIR")
    if is_nx_home(env):
        return env

    seen = set()
    for root in NX_ROOTS:
        if not os.path.isdir(root):
            continue
        candidates = [root]
        try:
            for name in sorted(os.listdir(root)):
                full = os.path.join(root, name)
                if os.path.isdir(full):
                    candidates.append(full)
                    try:
                        for inner in sorted(os.listdir(full)):
                            deep = os.path.join(full, inner)
                            if os.path.isdir(deep):
                                candidates.append(deep)
                    except Exception:
                        pass
        except Exception:
            pass
        for candidate in candidates:
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            if is_nx_home(candidate):
                return candidate

    # 最后一招：扫所有盘符的一层子目录
    for letter in "CDEFGH":
        drive = letter + ":\\"
        if not os.path.isdir(drive):
            continue
        if is_nx_home(drive):
            return drive
        try:
            for name in sorted(os.listdir(drive)):
                full = os.path.join(drive, name)
                if is_nx_home(full):
                    return full
        except Exception:
            continue

    raise RuntimeError(
        "找不到 NX 安装。用 --nx-home 指定，或设 UGII_BASE_DIR 环境变量"
        "（需要那个目录下有 NXBIN\\run_journal.exe）")


def run_journal(nx_home, journal_path, timeout=1800, extra_env=None, cwd=None,
                on_line=None):
    """用 run_journal.exe 跑一个 journal，返回 (returncode, stdout, stderr)。

    on_line 不为空时改成流式读取：每读到一行就回调一次，GUI 才能实时显示进度
    （NX 一次出图 20~40 秒，全憋到结束才显示的话界面像死了一样）。
    """
    runner = os.path.join(nx_home, "NXBIN", "run_journal.exe")
    if not os.path.isfile(runner):
        raise RuntimeError("run_journal.exe not found at %s" % runner)
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    if on_line is None:
        process = subprocess.run(
            [runner, journal_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd or os.path.dirname(journal_path),
            timeout=timeout,
        )
        out = process.stdout.decode("utf-8", errors="replace")
        err = process.stderr.decode("utf-8", errors="replace")
        return process.returncode, out, err

    # 流式：stdout/stderr 合流，逐行回调并累积
    process = subprocess.Popen(
        [runner, journal_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=cwd or os.path.dirname(journal_path),
        bufsize=1,
    )
    collected = []
    deadline = time.time() + timeout
    try:
        for raw in iter(process.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            collected.append(line)
            if on_line is not None:
                text = line.rstrip("\r\n")
                if text.startswith(LOG_PREFIX):
                    on_line(text[len(LOG_PREFIX):])
                elif text.startswith(RESULT_PREFIX):
                    pass
                elif text.strip():
                    on_line(text)
            if time.time() > deadline:
                process.kill()
                raise RuntimeError("NX journal timed out after %s s" % timeout)
    finally:
        try:
            process.stdout.close()
        except Exception:
            pass
        try:
            process.wait(timeout=30)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    return process.returncode or 0, "".join(collected), ""


def parse_result(stdout):
    """从 run_journal 的输出里摘出结果 JSON 和日志行。"""
    logs = []
    payload = None
    for line in stdout.splitlines():
        if line.startswith(LOG_PREFIX):
            logs.append(line[len(LOG_PREFIX):])
        elif line.startswith(RESULT_PREFIX):
            try:
                payload = json.loads(line[len(RESULT_PREFIX):])
            except Exception:
                payload = None
    return payload, logs


JOURNAL_TEMPLATE = '''# -*- coding: utf-8 -*-
# 由 nxdraft 自动生成：在 NX batch 会话里跑一串 nxdraft 命令，把结果打到 stdout。
import json
import os
import sys
import traceback

RESULT_PREFIX = %(result_prefix)r
LOG_PREFIX = %(log_prefix)r

# 让 journal 能 import 到 nxdraft
%(sys_path_insert)s

def emit(kind, text):
    sys.stdout.write(kind + text + "\\n")
    sys.stdout.flush()

def main():
    from nxdraft import Runtime
    import nxdraft.api as api

    rt = Runtime(out_dir=%(out_dir)r, log=lambda text: emit(LOG_PREFIX, text))

    # NX 起会话时不带部件，命令里的路径要先打开成显示部件
    for path in %(open_paths)r:
        emit(LOG_PREFIX, "opening " + path)
        api.open_part(rt, path)

    # steps 是一串 [函数名, 参数]；同一个 NX 会话里连续跑完，
    # 因为第二次调用 NX 时前一次的图纸已经不在内存里了。
    steps = json.loads(%(steps)r)
    results = {}
    for name, params in steps:
        emit(LOG_PREFIX, "-> " + name)
        handler = getattr(api, name, None)
        if handler is None:
            raise RuntimeError("unknown command: %%s" %% name)
        results[name] = handler(rt, **params)
        emit(LOG_PREFIX, "<- " + name + " ok")

    emit(RESULT_PREFIX, json.dumps({"ok": True, "results": results}, ensure_ascii=False, default=str))

try:
    main()
except Exception as error:
    emit(RESULT_PREFIX, json.dumps({
        "ok": False,
        "error": "%%s: %%s" %% (type(error).__name__, error),
        "trace": traceback.format_exc(),
    }, ensure_ascii=False))
'''


def package_root():
    """nxdraft 包所在的**父目录**，要注入到 NX journal 的 sys.path 里。

    两种运行方式：

    - 源码目录直接跑：包在 <repo>/nxdraft，父目录是 <repo>。
    - PyInstaller 单文件 exe：代码被塞进 exe 内部，NX 的 Python 根本 import 不到。
      所以打包时把整个 nxdraft 目录作为 datas 释放到 sys._MEIPASS，这里返回
      那个临时目录。见 build.spec 与 bundle_for_journal()。
    """
    # PyInstaller 运行时：解压目录在 sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = os.path.join(meipass, "nxdraft")
        if os.path.isdir(candidate):
            return meipass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_for_journal():
    """exe 模式下，NX 的 journal 需要能 import nxdraft —— 确认包在解压目录里。

    返回可用的 sys.path 目录；包不在（比如源码运行）时返回源码父目录。
    """
    root = package_root()
    if not os.path.isdir(os.path.join(root, "nxdraft")):
        raise RuntimeError(
            "找不到 nxdraft 包目录（期望 %s\\nxdraft）。"
            "exe 打包时要把 nxdraft 目录作为 datas 带上。" % root)
    return root


def run_steps(nx_home, steps, out_dir, package_root_dir=None, timeout=1800,
              keep_journal=False, open_paths=None, log_callback=None):
    """在**同一个** NX 会话里按顺序跑多个 api 函数。

    steps 是 [(函数名, 参数字典), ...]。返回 {函数名: 结果}。

    为什么需要这个：NX 每个进程只有一个会话，进程退出图纸就没了。所以
    「出图 → 标注 → 打印 PDF」必须一次跑完，分三次调用会得到
    "the part has no drafting views yet"。
    """
    if package_root_dir is None:
        package_root_dir = bundle_for_journal()
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    insert = "sys.path.insert(0, %r)" % package_root_dir
    journal = JOURNAL_TEMPLATE % {
        "result_prefix": RESULT_PREFIX,
        "log_prefix": LOG_PREFIX,
        "sys_path_insert": insert,
        "out_dir": out_dir,
        "steps": json.dumps([[name, params] for name, params in steps], ensure_ascii=False),
        "open_paths": [os.path.abspath(p) for p in (open_paths or [])],
    }

    handle, path = tempfile.mkstemp(suffix=".py", prefix="nxdraft_journal_")
    os.close(handle)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(journal)

    started = time.time()
    try:
        code, stdout, stderr = run_journal(nx_home, path, timeout=timeout,
                                           extra_env={"PYTHONIOENCODING": "utf-8"},
                                           on_line=log_callback)
    finally:
        if not keep_journal:
            try:
                os.remove(path)
            except Exception:
                pass

    payload, logs = parse_result(stdout)
    elapsed = round(time.time() - started, 1)
    if payload is None:
        detail = (stderr or "").strip() or (stdout or "").strip()
        raise RuntimeError(
            "NX journal produced no result (exit %s, %.1fs).\\n%s" % (code, elapsed, detail[-3000:]))
    if not payload.get("ok"):
        raise RuntimeError("%s\\n%s" % (payload.get("error"), payload.get("trace", "")))
    return payload.get("results") or {}


def run_command(nx_home, command, params, out_dir, package_root_dir=None,
                timeout=1800, keep_journal=False, open_paths=None,
                log_callback=None):
    """跑单个 api 函数，返回它的结果（不是 dict 包装）。

    open_paths 里的 .prt 会在命令执行前依次打开（NX 起会话时没有当前部件）。
    log_callback 收到每一行 NX 侧日志（GUI 用它做实时进度）。
    """
    results = run_steps(nx_home, [(command, params)], out_dir,
                        package_root_dir=package_root_dir, timeout=timeout,
                        keep_journal=keep_journal, open_paths=open_paths,
                        log_callback=log_callback)
    return results.get(command), []
