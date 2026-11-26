"""公开 API：把提取出来的制图函数包装成好用的入口。

提取模块里的函数都叫 cmd_xxx(args)，参数是个 dict——那是给 JSON 命令队列用的
形状。这里给出一层命名更直白的函数，CLI 和队列都走它，避免调用方去拼 dict。

所有函数第一个参数都是 Runtime。
"""

import os

from .runtime import Runtime


def _call(rt, name, args):
    """调用提取模块里的一个 cmd_* 函数。"""
    module = rt.module
    handler = getattr(module, name, None)
    if handler is None:
        raise RuntimeError("%s is not available in this build" % name)
    return handler(args)


# ---- 出图 ----


def drawing(rt, size="A3", projection="first", views=None, auto_scale=True,
            section="replace", part_name=None, material=None, drawing_no=None,
            designer=None, checker=None, company=None, frame=True,
            min_view_area=None, view_gap=None, template=False, sheet_name=None):
    """在活动部件上出一张图纸：视图 + 布局 + GB 图框。

    views 传 {"front": True, "top": True, ...} 可指定；不传则按 minimal_views()
    自动选「能标全尺寸的最少视图」。
    """
    args = {
        "size": size,
        "projection": projection,
        "autoScale": auto_scale,
        "section": section,
        "frame": frame,
        "template": template,
    }
    if views is not None:
        args["views"] = views
    if part_name:
        args["partName"] = part_name
    if material:
        args["material"] = material
    if drawing_no:
        args["drawingNo"] = drawing_no
    if designer:
        args["designer"] = designer
    if checker:
        args["checker"] = checker
    if company:
        args["company"] = company
    if min_view_area is not None:
        args["minViewArea"] = min_view_area
    if view_gap is not None:
        args["viewGap"] = view_gap
    if sheet_name:
        args["sheetName"] = sheet_name
    return _call(rt, "cmd_drawing", args)


def dimension(rt, views=None, length=True, diameter=True, count=True, step=10,
              text_size=3.5):
    """在已有图纸上打原生尺寸（读数随模型联动）。"""
    args = {"length": length, "diameter": diameter, "count": count,
            "step": step, "textSize": text_size}
    if views is not None:
        args["views"] = views
    return _call(rt, "cmd_dimension", args)


def pdf(rt, path=None, sheets=None):
    """把图纸打印成 PDF。path 不给就落到 out_dir/<部件名>.pdf。"""
    if not path:
        leaf = rt.display_part().Leaf
        path = rt.out_dir + "\\" + _safe_name(str(leaf)) + ".pdf"
    args = {"path": path}
    if sheets:
        args["sheets"] = sheets
    return _call(rt, "cmd_pdf", args)


def section_view(rt, start, end, parent=None, kind="pointtopoint", at=None):
    """加一个剖视图。start/end 是剖切线两端（模型坐标 [x,y,z]）。"""
    args = {"start": list(start), "end": list(end), "type": kind}
    if parent:
        args["parent"] = parent
    if at:
        args["at"] = list(at)
    return _call(rt, "cmd_section_view", args)


def section_vertical(rt, cut_x, parent=None, scale=None, at=None,
                     arrow_dir=None, step_dir=None):
    """竖直剖：在模型 X = cut_x 处切一刀。"""
    args = {"cutX": cut_x}
    if parent:
        args["parent"] = parent
    if scale is not None:
        args["scale"] = scale
    if at:
        args["at"] = list(at)
    if arrow_dir:
        args["arrowDir"] = list(arrow_dir)
    if step_dir:
        args["stepDir"] = list(step_dir)
    return _call(rt, "cmd_section_vertical", args)


def detail_view(rt, radius, center=None, parent=None, scale=2, shape="circular", at=None):
    """局部放大图。⚠️ batch 会话里放大内容印不出来，只留边界圈，作占位用。"""
    args = {"radius": radius, "scale": scale, "shape": shape}
    if center:
        args["center"] = list(center)
    if parent:
        args["parent"] = parent
    if at:
        args["at"] = list(at)
    return _call(rt, "cmd_detail_view", args)


def frame(rt, part_name=None, material=None, drawing_no=None, designer=None,
          checker=None, company=None, date=None, scale=None, margin=None,
          binding=None, title_width=None, title_height=None):
    """在现有图纸上画 GB 图框 + 标题栏（真实曲线，会随 PDF 打印）。"""
    args = {}
    for key, value in (("partName", part_name), ("material", material),
                       ("drawingNo", drawing_no), ("designer", designer),
                       ("checker", checker), ("company", company), ("date", date),
                       ("scale", scale), ("margin", margin), ("binding", binding),
                       ("titleWidth", title_width), ("titleHeight", title_height)):
        if value is not None:
            args[key] = value
    return _call(rt, "cmd_drawing_frame", args)


# ---- 注释类 ----


def note(rt, text, at=(20, 275), size=3.5, font=None):
    """图纸上写一段文字（中文自动用 CJK 字体）。"""
    args = {"text": text, "at": list(at), "size": size}
    if font:
        args["font"] = font
    return _call(rt, "cmd_note", args)


def gdt(rt, characteristic="position", tolerance="0.05", at=None, view=None):
    """形位公差框。"""
    args = {"characteristic": characteristic, "tolerance": tolerance}
    if at:
        args["at"] = list(at)
    if view:
        args["view"] = view
    return _call(rt, "cmd_gdt", args)


def balloon(rt, number="1", at=None, view=None, size=5, text=None):
    """序号气球。"""
    args = {"number": number, "size": size}
    if at:
        args["at"] = list(at)
    if view:
        args["view"] = view
    if text:
        args["text"] = list(text)
    return _call(rt, "cmd_balloon", args)


def surface_finish(rt, value="3.2", at=None, view=None, method=None):
    """表面粗糙度符号。"""
    args = {"value": value}
    if at:
        args["at"] = list(at)
    if view:
        args["view"] = view
    if method:
        args["method"] = method
    return _call(rt, "cmd_surface_finish", args)


def centerline(rt, at, view=None):
    """中心标记。"""
    args = {"at": list(at)}
    if view:
        args["view"] = view
    return _call(rt, "cmd_centerline", args)


# ---- 视图控制 ----


def view_scale(rt, scale=None, ratio=None, view=None):
    """设置某个视图的出图比例。"""
    args = {}
    if ratio:
        args["ratio"] = ratio
    elif scale is not None:
        args["scale"] = scale
    if view:
        args["view"] = view
    return _call(rt, "cmd_view_scale", args)


def view_move(rt, at, view=None):
    """把某个视图移到图纸上的指定位置。"""
    args = {"at": list(at)}
    if view:
        args["view"] = view
    return _call(rt, "cmd_view_move", args)


def sheet_delete(rt, name=None):
    """删一张图纸页。"""
    args = {}
    if name:
        args["name"] = name
    return _call(rt, "cmd_sheet_delete", args)


# ---- 明细表 ----


def bom(rt, csv=None, fast=False, include_suppressed=False):
    """装配明细表汇总（按零件汇总数量/体积/质量）。"""
    args = {"fast": fast, "includeSuppressed": include_suppressed}
    if csv:
        args["csv"] = csv
    return _call(rt, "cmd_assembly_bom", args)


def bom_table(rt, columns=None, language="zh", row_height=7, text_size=3,
              at=None, caption=None, csv=None, fast=False, include_suppressed=False,
              drawing=True, size="A3"):
    """在图纸上画明细表（真实表格线 + 每格文字）。"""
    args = {"language": language, "rowHeight": row_height, "textSize": text_size,
            "fast": fast, "includeSuppressed": include_suppressed,
            "drawing": drawing, "size": size}
    if columns:
        args["columns"] = columns
    if at:
        args["at"] = list(at)
    if caption:
        args["caption"] = caption
    if csv:
        args["csv"] = csv
    return _call(rt, "cmd_bom_table", args)


# ---- 批量出图 ----


def batch_drawings(rt, paths=None, out_dir=None, size=None, projection="first",
                   views=None, reuse=False, from_assembly=True, dimension=True,
                   length=True, diameter=True, order=None, number_prefix="",
                   number_start=1, number_digits=0, material=None,
                   assembly_drawing=True):
    """对装配的所有零件（或给定文件列表）逐件出图并打印 PDF。

    参数
    ----
    paths        : 显式给零件文件列表（不给就从装配取）
    order        : 图纸顺序 —— 零件文件名（或路径）列表，按这个顺序出图；
                   不在列表里的排在后面，保持装配原顺序
    number_prefix: 图号前缀，如 "DSP-"
    number_start : 零件的起始图号（装配图固定 0）
    number_digits: 补零位数（0 = 不补）
    length/diameter : 零件图要不要标线性 / 直径尺寸
    material     : 统一写进标题栏的材料（不给就用零件自身属性）
    assembly_drawing : 是否先出装配图（图号 0）

    会保护装配引用：SaveAs 之后自动把组件引用恢复到原路径。
    """
    args = {
        "size": size,
        "projection": projection,
        "reuse": reuse,
        "fromAssembly": from_assembly,
        "dimension": dimension,
        "length": length,
        "diameter": diameter,
        "outDir": out_dir or rt.out_dir,
        "order": list(order) if order else None,
        "numberPrefix": number_prefix or "",
        "numberStart": number_start,
        "numberDigits": number_digits,
        "assemblyDrawing": assembly_drawing,
    }
    if paths:
        args["paths"] = list(paths)
    if views is not None:
        args["views"] = views
    if material:
        args["material"] = material
    return _call(rt, "cmd_batch_drawings", args)


def assembly_overview(rt, path=None):
    """分析装配：零件数量、名称、顺序，给界面用来手动排列图纸顺序。

    只有 NX 才能读组件顺序与材料，所以这个函数要在 journal 里跑。
    """
    rollup = getattr(rt.module, "bom_rollup")
    part = rt.display_part()
    assembly = getattr(part, "ComponentAssembly", None)
    if assembly is None:
        raise RuntimeError("%s is not an assembly" % getattr(part, "Leaf", "?"))
    data = rollup(assembly, True, False)
    groups = data.get("groups") or []
    items = []
    for index, row in enumerate(groups):
        items.append({
            "index": index,
            "item": row.get("item") or (index + 1),
            "name": row.get("name") or "",
            "file": row.get("file") or "",
            "quantity": row.get("quantity") or 1,
            "material": row.get("material") or "",
        })
    leaf = os.path.splitext(os.path.basename(path))[0] if path else (
        os.path.splitext(str(getattr(part, "Leaf", "")))[0])
    return {
        "assembly": leaf,
        "count": len(items),
        "items": items,
    }


# ---- 打开 / 保存 ----


def open_part(rt, path):
    """打开一个 .prt 并设为显示部件。"""
    handler = getattr(rt.module, "open_path")
    part = handler(path)
    rt.state["part"] = part
    return part


def _safe_name(text):
    for bad in '\\/:*?"<>|':
        text = text.replace(bad, "_")
    return text.strip(" .") or "part"
