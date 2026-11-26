"""nxdraft 命令行入口。

每次调用都在一个独立的 NX batch 会话里跑（NX 不能在同一进程里复用），
所以命令的设计是「一次调用做完一件事」。

    nxdraft drawing <part.prt> --out DIR --size A3 --pdf
    nxdraft batch   <asm.prt>  --out DIR --size A4
    nxdraft dimension <part.prt> --out DIR --views Top,Front
    nxdraft bom     <asm.prt>  --out DIR --csv

`serve` 子命令提供 JSON 命令队列，给别的程序调用（同 dsh-nx 的协议形状）。
"""

import argparse
import json
import os
import sys

from . import launcher, options


def _print_logs(logs, quiet=False):
    if quiet:
        return
    for line in logs:
        print("  " + line)


def cmd_drawing(args):
    """出图。默认一次会话里做完 出图 → 标注 → PDF。

    这三步不能拆成三次进程调用：NX 进程退出后图纸不在内存里，
    第二次调用会报 "the part has no drafting views yet"。
    """
    params = {
        "size": args.size,
        "projection": args.projection,
        "auto_scale": args.auto_scale,
    }
    if args.views:
        flags, unknown = options.views_to_flags(
            [name.strip() for name in args.views.split(",") if name.strip()])
        if unknown:
            raise RuntimeError("认不出的视图名: %s（可用：%s）" % (
                "、".join(unknown),
                "、".join(label for _k, label, _n in options.VIEW_NAMES)))
        if flags:
            params["views"] = flags
    if args.material:
        params["material"] = args.material
    if args.drawing_no:
        params["drawing_no"] = args.drawing_no
    if args.designer:
        params["designer"] = args.designer

    steps = [("drawing", params)]
    if not args.no_dimension:
        dim_params = {"length": not args.no_length, "diameter": not args.no_diameter}
        if args.dim_views:
            dim_params["views"] = [n.strip() for n in args.dim_views.split(",") if n.strip()]
        steps.append(("dimension", dim_params))

    pdf_path = args.pdf
    if pdf_path is None and args.with_pdf:
        leaf = os.path.splitext(os.path.basename(args.part))[0]
        pdf_path = os.path.join(args.out, leaf + ".pdf")
    if pdf_path:
        steps.append(("pdf", {"path": pdf_path}))

    results = launcher.run_steps(
        args.nx_home, steps, args.out, timeout=args.timeout,
        open_paths=[args.part],
        log_callback=None if args.quiet else (lambda line: print("  " + line)))
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_pdf(args):
    params = {"path": args.pdf}
    if args.sheets:
        params["sheets"] = [s.strip() for s in args.sheets.split(",") if s.strip()]
    result, logs = launcher.run_command(
        args.nx_home, "pdf", params, args.out, timeout=args.timeout, open_paths=[args.part])
    _print_logs(logs, args.quiet)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_dimension(args):
    params = {"length": not args.no_length, "diameter": not args.no_diameter}
    if args.views:
        params["views"] = [name.strip() for name in args.views.split(",") if name.strip()]
    result, logs = launcher.run_command(
        args.nx_home, "dimension", params, args.out, timeout=args.timeout, open_paths=[args.part])
    _print_logs(logs, args.quiet)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_batch(args):
    params = {
        "size": args.size,
        "reuse": args.reuse,
        "out_dir": args.out,
        "number_prefix": args.no_prefix or "",
        "number_start": args.no_start,
        "number_digits": args.no_digits,
        "length": not args.no_length,
        "diameter": not args.no_diameter,
        "assembly_drawing": not args.no_assembly_drawing,
    }
    if args.paths:
        params["paths"] = args.paths
    if args.order:
        params["order"] = [name.strip() for name in args.order.split(",") if name.strip()]
    if args.material:
        params["material"] = args.material
    result, _logs = launcher.run_command(
        args.nx_home, "batch_drawings", params, args.out, timeout=args.timeout,
        open_paths=([args.assembly] if args.assembly else []),
        log_callback=None if args.quiet else (lambda line: print("  " + line)))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_analyze(args):
    """分析装配：列出零件名称与顺序，用于手动排图纸顺序。"""
    result, _logs = launcher.run_command(
        args.nx_home, "assembly_overview", {"path": args.assembly}, args.out,
        timeout=args.timeout, open_paths=[args.assembly],
        log_callback=None if args.quiet else (lambda line: print("  " + line)))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_bom(args):
    params = {"csv": args.csv} if args.csv else {}
    result, logs = launcher.run_command(
        args.nx_home, "bom", params, args.out, timeout=args.timeout, open_paths=[args.assembly])
    _print_logs(logs, args.quiet)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_serve(args):
    """JSON 命令队列：别的程序往 req/ 投 JSON，我们从 resp/ 收结果。"""
    from . import queue

    return queue.serve(args.req, args.resp, args.out, args.nx_home,
                       poll=args.poll, timeout=args.timeout, verbose=args.quiet is False)


def cmd_gui(args):
    """起本地 Web 界面。"""
    from . import gui

    return gui.run(args.nx_home, args.out, host=args.host, port=args.port,
                   open_browser=not args.no_browser)


def build_parser():
    # 全局参数（--nx-home / --out / --timeout / --quiet）既接受写在子命令前面，
    # 也接受写在后面。argparse 默认只认前者，而习惯上大家会写成
    # `nxdraft drawing part.prt --out DIR`，所以每个子命令再挂一份，
    # 用同一批 dest，后出现的覆盖先出现的。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--nx-home", help="NX 安装根目录（含 NXBIN\\run_journal.exe）")
    common.add_argument("--out", help="产物目录")
    common.add_argument("--timeout", type=float, help="单次调用超时秒数")
    common.add_argument("--quiet", action="store_true", default=None, help="不打印 NX 侧日志")

    parser = argparse.ArgumentParser(
        prog="nxdraft",
        description="Siemens NX 制图工具（独立版，不依赖 DSH）",
        parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    # drawing
    p = sub.add_parser("drawing", help="出零件图：视图 + 布局 + GB 图框", parents=[common])
    p.add_argument("part", help=".prt 文件路径")
    p.add_argument("--size", default="A3", help="图纸幅面 A4/A3/A2/A1/A0")
    p.add_argument("--projection", default="first", choices=["first", "third"])
    p.add_argument("--views", help="逗号分隔的视图名，如 Top,Front（默认自动选最少视图）")
    p.add_argument("--material", help="标题栏材料")
    p.add_argument("--drawing-no", help="标题栏图号")
    p.add_argument("--designer", help="标题栏设计")
    p.add_argument("--no-auto-scale", dest="auto_scale", action="store_false", default=True)
    p.add_argument("--with-pdf", action="store_true", help="同时打印 PDF 到产物目录")
    p.add_argument("--pdf-path", dest="pdf", help="PDF 输出路径（隐含 --with-pdf）")
    p.add_argument("--no-dimension", dest="no_dimension", action="store_true",
                   help="只出视图，不标尺寸")
    p.add_argument("--no-length", dest="no_length", action="store_true", help="不标线性尺寸")
    p.add_argument("--no-diameter", dest="no_diameter", action="store_true", help="不标直径")
    p.add_argument("--dim-views", dest="dim_views", help="标注用的视图（逗号分隔）")
    p.set_defaults(func=cmd_drawing)

    # pdf
    p = sub.add_parser("pdf", help="把已有图纸打印成 PDF", parents=[common])
    p.add_argument("part", help=".prt 文件路径")
    p.add_argument("--pdf", help="输出 PDF 路径")
    p.add_argument("--sheets", help="逗号分隔的图纸名")
    p.set_defaults(func=cmd_pdf)

    # dimension
    p = sub.add_parser("dimension", help="在已有图纸上打原生尺寸", parents=[common])
    p.add_argument("part", help=".prt 文件路径")
    p.add_argument("--views", help="逗号分隔的视图名")
    p.add_argument("--no-length", dest="no_length", action="store_true")
    p.add_argument("--no-diameter", dest="no_diameter", action="store_true")
    p.set_defaults(func=cmd_dimension)

    # batch
    p = sub.add_parser("batch", help="批量出图：装配的所有零件，或指定文件列表", parents=[common])
    p.add_argument("assembly", nargs="?", help=".prt 装配文件（省略则用 --paths）")
    p.add_argument("--paths", nargs="*", help="直接给零件文件列表")
    p.add_argument("--size", default="A4", help="图纸幅面")
    p.add_argument("--reuse", action="store_true", help="复用已有图纸页")
    p.add_argument("--order", help="图纸顺序：逗号分隔的零件名，如 泵体,阀体,销轴")
    p.add_argument("--no-prefix", dest="no_prefix", help="图号前缀，如 DSP-")
    p.add_argument("--no-start", dest="no_start", type=int, default=1,
                   help="零件起始图号（装配图固定 0）")
    p.add_argument("--no-digits", dest="no_digits", type=int, default=0,
                   help="图号补零位数（0=不补）")
    p.add_argument("--material", help="统一写入标题栏的材料")
    p.add_argument("--no-length", dest="no_length", action="store_true",
                   help="零件图不标线性尺寸")
    p.add_argument("--no-diameter", dest="no_diameter", action="store_true",
                   help="零件图不标直径尺寸")
    p.add_argument("--no-assembly-drawing", dest="no_assembly_drawing",
                   action="store_true", help="不先出装配图（图号 0）")
    p.set_defaults(func=cmd_batch)

    # analyze
    p = sub.add_parser("analyze", help="分析装配：列出零件名称与顺序", parents=[common])
    p.add_argument("assembly", help=".prt 装配文件")
    p.set_defaults(func=cmd_analyze)

    # bom
    p = sub.add_parser("bom", help="装配明细表汇总", parents=[common])
    p.add_argument("assembly", help=".prt 装配文件")
    p.add_argument("--csv", help="同时写 CSV")
    p.set_defaults(func=cmd_bom)

    # serve
    p = sub.add_parser("serve", help="JSON 命令队列服务", parents=[common])
    p.add_argument("--req", required=True, help="请求目录")
    p.add_argument("--resp", required=True, help="响应目录")
    p.add_argument("--poll", type=float, default=0.05, help="轮询间隔秒")
    p.set_defaults(func=cmd_serve)

    # gui
    p = sub.add_parser("gui", help="本地 Web 图形界面", parents=[common])
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只本机）")
    p.add_argument("--port", type=int, default=0, help="端口（0 = 自动挑空闲端口）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_gui)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # 全局参数的默认值（子命令那份可能是 None）
    if args.out is None:
        args.out = os.path.join(os.getcwd(), "nxdraft-out")
    if args.timeout is None:
        args.timeout = 1800.0
    if args.quiet is None:
        args.quiet = False

    try:
        args.nx_home = launcher.find_nx_home(args.nx_home)
    except RuntimeError as error:
        print("错误: %s" % error, file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except RuntimeError as error:
        print("失败: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
