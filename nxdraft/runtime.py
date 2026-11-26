"""运行时：把 NX 会话和公用小工具注入到提取出来的制图函数里。

提取出来的 100 个函数原本是 nxworker.py 里 main() 的闭包，靠闭包捕获 NX、s、
state、out 这些名字。去掉闭包以后，这些名字必须成为**模块级全局变量**才能被
函数体看到——Python 的查找顺序是「局部 → 全局 → 内置」，所以只要在本模块里定义
它们，再把它们注入到 _extracted 模块的命名空间，那些函数就能正常工作。

用法（在 NX 的 batch 会话里）：

    from nxdraft import runtime
    rt = runtime.Runtime(out_dir=r"D:\\out")
    rt.install()                      # 注入到 _extracted
    from nxdraft import api
    api.batch_drawings(rt, {"paths": [...], "outDir": r"D:\\out"})
"""

import os
import time

# 图纸尺寸（mm，宽 x 高），与 NX 的图纸页一致
SHEET_SIZES = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}

# 形位公差特性名 -> NXOpen 枚举成员名
FCF_CHARACTERISTICS = {
    "position": "Position", "flatness": "Flatness", "straightness": "Straightness",
    "circularity": "Circularity", "cylindricity": "Cylindricity",
    "perpendicularity": "Perpendicularity", "parallelism": "Parallelism",
    "angularity": "Angularity", "concentricity": "Concentricity", "symmetry": "Symmetry",
    "profile_line": "ProfileOfALine", "profile_surface": "ProfileOfASurface",
    "circular_runout": "CircularRunout", "total_runout": "TotalRunout",
}


def fix_text(value):
    """NX 有时把 str 包装成自己的类型；统一成普通 str。"""
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


class Runtime(object):
    """一个 NX batch 会话里的制图运行时。

    out_dir 是产物根目录（PDF/PRT 默认落这里）；log 是可选的日志回调，
    不传就写到内存列表里，CLI 结束时统一打印。
    """

    def __init__(self, out_dir=None, log=None, log_file=None):
        import NXOpen

        self.NXOpen = NXOpen
        self.NX = NXOpen
        self.s = NXOpen.Session.GetSession()
        self.state = {"part": None}
        self.started = time.time()
        self.out_dir = os.path.abspath(out_dir) if out_dir else os.path.join(
            os.path.expanduser("~"), ".nxdraft", "out")
        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir)
        self.log_file = log_file
        self.lines = []
        self._log = log
        self.installed = False

    # ---- 日志 ----

    def out(self, *parts):
        text = " ".join(str(p) for p in parts)
        self.lines.append(text)
        if self._log is not None:
            try:
                self._log(text)
            except Exception:
                pass
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as handle:
                    handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
            except Exception:
                pass
        return text

    # ---- 当前部件 ----

    def display_part(self):
        """当前显示的部件。NX 的真相在 Session.Parts.Display，缓存只是兜底。"""
        part = self.s.Parts.Display
        if part is not None:
            self.state["part"] = part
            return part
        cached = self.state.get("part")
        if cached is not None:
            return cached
        work = self.s.Parts.Work
        if work is not None:
            self.state["part"] = work
            return work
        raise RuntimeError("no part is open in this NX session")

    def current_part(self):
        return self.display_part()

    # ---- 注入 ----

    def globals_map(self):
        """要注入到提取模块命名空间里的名字。

        除了 NX 会话对象，还要把函数体里直接引用的标准库模块一起注入：
        提取出来的代码写着 `math.radians(...)`、`re.sub(...)`，但在 nxworker.py
        里那些 import 位于 main() 顶部，属于闭包可见的名字，去掉闭包后就找不到了。
        """
        import math
        import re
        return {
            "NX": self.NX,
            "NXOpen": self.NXOpen,
            "s": self.s,
            "state": self.state,
            "OUT_DIR": self.out_dir,
            "SHEET_SIZES": SHEET_SIZES,
            "FCF_CHARACTERISTICS": FCF_CHARACTERISTICS,
            "fix_text": fix_text,
            "out": self.out,
            "display_part": self.display_part,
            "time": time,
            "os": os,
            "math": math,
            "re": re,
        }

    def install(self):
        """把运行时名字写进 _extracted 的模块全局。重复调用是安全的。"""
        from . import _extracted

        for name, value in self.globals_map().items():
            setattr(_extracted, name, value)
        self.installed = True
        return _extracted

    @property
    def module(self):
        from . import _extracted
        if not self.installed:
            self.install()
        return _extracted
