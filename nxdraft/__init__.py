"""nxdraft —— 从 dsh-nx 抽出来的 NX 制图工具，不依赖 DSH。

能力：零件图（视图 + 原生尺寸 + GB 图框 + PDF）、装配图（Top/剖视 + 明细表）、
批量出图（逐件 A3/A4 + PDF，带装配引用保护）。

用法（必须在 NX 的 batch 会话里跑，普通 Python 拿不到 NXOpen）：

    # 命令行
    nxdraft drawing "E:\\parts\\泵体.prt" --out D:\\out --size A3 --pdf
    nxdraft batch "E:\\asm\\装配.prt" --out D:\\out

    # Python
    from nxdraft import Runtime, api
    rt = Runtime(out_dir=r"D:\\out")
    api.drawing(rt, size="A3")
    api.dimension(rt)
    api.pdf(rt)

NX 会话怎么起：见 nxdraft.launcher（调 NXBIN\\run_journal.exe）。
"""

from .runtime import Runtime, SHEET_SIZES, FCF_CHARACTERISTICS, fix_text
from . import api

__all__ = ["Runtime", "api", "SHEET_SIZES", "FCF_CHARACTERISTICS", "fix_text"]
__version__ = "1.0.0"
