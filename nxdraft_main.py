"""exe 入口。

双击运行时（没有命令行参数）直接开 GUI；带参数时当成 CLI 用。
PyInstaller 的入口指向这个文件，而不是 nxdraft.cli，就是为了「双击即用」。

    nxdraft.exe                     → 打开图形界面
    nxdraft.exe drawing part.prt    → 命令行出图
"""

import sys


def main():
    # 双击 exe 时 sys.argv 只有一个元素；此时默认走 GUI
    if len(sys.argv) == 1:
        sys.argv = [sys.argv[0], "gui"]
    from nxdraft.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
