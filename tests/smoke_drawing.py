# -*- coding: utf-8 -*-
"""在 NX batch 会话里跑完整制图流程：出图 + 标注 + PDF。

这个是 nxdraft 的端到端自测脚本，不经过 launcher，直接由 run_journal.exe 执行。

    run_journal.exe _nx_journal.py <part.prt> <out_dir>
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
        raise SystemExit("用法: run_journal.exe smoke_drawing.py <part.prt> [out_dir]")
    part_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.expanduser("~"), "nxdraft-smoke")

    from nxdraft import Runtime
    import nxdraft.api as api

    rt = Runtime(out_dir=out_dir, log=lambda text: emit(LOG_PREFIX, text))
    api.open_part(rt, part_path)

    drawing = api.drawing(rt, size="A3")
    dimension = api.dimension(rt)
    leaf = os.path.splitext(os.path.basename(part_path))[0]
    printed = api.pdf(rt, path=os.path.join(out_dir, leaf + ".pdf"))

    emit(RESULT_PREFIX, json.dumps({
        "ok": True,
        "views": [v.get("name") for v in drawing["sheet"]["views"]],
        "dimensions": dimension.get("dimensions"),
        "skipped": len(dimension.get("skipped") or []),
        "pdf": printed.get("path"),
        "pdfBytes": printed.get("bytes"),
    }, ensure_ascii=False, default=str))


try:
    main()
except Exception as error:
    import traceback
    emit(RESULT_PREFIX, json.dumps({
        "ok": False,
        "error": "%s: %s" % (type(error).__name__, error),
        "trace": traceback.format_exc(),
    }, ensure_ascii=False))
