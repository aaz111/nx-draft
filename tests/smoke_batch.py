# -*- coding: utf-8 -*-
"""端到端自测：批量出图 + 装配明细表。

    run_journal.exe _nx_journal_batch.py <assembly.prt> <out_dir>
"""

import json
import os
import sys

RESULT_PREFIX = "@@NXDRAFT_RESULT@@ "
LOG_PREFIX = "@@NXDRAFT_LOG@@ "


def emit(kind, text):
    sys.stdout.write(kind + text + "\n")
    sys.stdout.flush()


def main():
    # 包目录 = 本脚本上一级（tests/ 的父目录），换机器不用改
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if package_root not in sys.path:
        sys.path.insert(0, package_root)

    if len(sys.argv) < 2:
        raise SystemExit("用法: run_journal.exe smoke_batch.py <assembly.prt> [out_dir]")
    asm_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.expanduser("~"), "nxdraft-smoke")

    from nxdraft import Runtime
    import nxdraft.api as api

    rt = Runtime(out_dir=out_dir, log=lambda text: emit(LOG_PREFIX, text))
    api.open_part(rt, asm_path)

    report = {}

    # 1) 明细表汇总
    try:
        report["bom"] = api.bom(rt)
    except Exception as error:
        report["bomError"] = "%s: %s" % (type(error).__name__, error)

    # 2) 批量出图
    try:
        batch = api.batch_drawings(rt, out_dir=os.path.join(out_dir, "batch"), size="A4")
        report["batch"] = {
            "parts": batch.get("parts"),
            "drawn": batch.get("drawn"),
            "dimensions": batch.get("dimensions"),
            "files": len(batch.get("files") or []),
            "failed": batch.get("failed"),
        }
    except Exception as error:
        report["batchError"] = "%s: %s" % (type(error).__name__, error)

    emit(RESULT_PREFIX, json.dumps({"ok": True, "report": report},
                                   ensure_ascii=False, default=str))


try:
    main()
except Exception as error:
    import traceback
    emit(RESULT_PREFIX, json.dumps({
        "ok": False,
        "error": "%s: %s" % (type(error).__name__, error),
        "trace": traceback.format_exc(),
    }, ensure_ascii=False))
