"""从 nxworker.py 的 main() 闭包里提取制图相关函数。

nxworker.py 把 9000 行代码全塞在 main() 里，所有功能函数都是闭包。
这个脚本按行号区间切出制图部分，把 4 空格缩进去掉，写成模块级函数。

用法：
    python extract.py <nxworker.py> <输出目录>
"""

import io
import os
import re
import sys

# 要提取的函数：按 main() 内的出现顺序
TARGETS = [
    # ---- 运行时基础设施（原闭包里的公用小工具） ----
    "fully_loaded", "bodies_of", "measure",
    # ---- 图纸与视图基础 ----
    "sheet_info", "current_sheet", "sheet_list", "views_on_sheet", "visible",
    "has_internal_features", "bores_visible_in_model_views", "bores_already_visible",
    "ensure_section_view", "section_cut_plane", "view_normal", "view_geometry",
    "minimal_views", "turned_about", "face_axis", "circle_features", "dominant_axis",
    "hole_axes", "arc_axis", "radial_axis",
    # ---- 出图主入口 ----
    "cmd_drawing", "cmd_pdf",
    # ---- 视图查询与放置 ----
    "drafting_views", "pick_view", "view_frame", "sheet_of", "boxes_overlap",
    "free_spot", "view_need", "view_summary",
    # ---- 剖视 / 局部放大 / 比例 / 移动 / 删图纸 ----
    "cmd_section_view", "cmd_detail_view", "uf_session", "ratio_value", "true_scale",
    "sheet_margins", "view_boxes", "order_views", "rank",
    # ---- 布局 ----
    "projection_alignment", "aligned_positions", "plan_grid",
    "layout_views_with_rules", "auto_layout_views",
    "cmd_view_scale", "cmd_view_move", "cmd_sheet_delete", "cmd_section_vertical",
    # ---- 注释类 ----
    "pnt", "annotation_view", "place_origin",
    "cmd_note", "cmd_gdt", "cmd_balloon", "cmd_surface_finish", "cmd_centerline",
    # ---- 投影与轮廓 ----
    "vertex_point", "drf", "edge_class", "view_axes", "matrix_axes", "is_orthographic",
    "view_sheet_point", "prototype_of", "assembly_bodies", "ortho_profile",
    "circle_faces_view", "longest",
    # ---- 尺寸标注 ----
    "drf_object", "drf_text", "dimension_style", "dimension_count_prefix",
    "place_dimension", "dim_text_box", "nearest_vertical_edge",
    "nearest_horizontal_edge", "dimension_extent", "dim_line_box", "cmd_dimension",
    "band_of", "corridor_spot", "place_diameter_as_linear", "edge_for_radius",
    "hide_datums",
    # ---- 图框 ----
    "cmd_drawing_frame",
    # ---- 装配遍历与明细表 ----
    "component_label", "assembly_parts", "component_pool", "component_bodies_in",
    "cmd_assembly_bom", "bom_rollup", "bom_columns", "text_width_mm", "fit_text",
    "sheet_line", "cell_text", "clear_previous_bom", "cjk_font", "cmd_bom_table",
    # ---- 批量出图（含装配引用保护） ----
    "open_path", "component_refs_of", "restore_component_reference", "cmd_batch_drawings",
]

DEF_RE = re.compile(r"^( +)def (\w+)\(")


def scan_functions(lines):
    """main() 内每个函数的 (名字, 起始行索引, 结束行索引, 缩进)，0 基、含首尾。

    有些函数是嵌套在别的函数里的内层函数（visible / rank / band_of /
    corridor_spot / place_diameter_as_linear / edge_for_radius），缩进 8 或 12。
    切区间时必须用"同层或更浅层"的下一个 def 作为边界，否则会把外层函数截断。
    """
    starts = []
    for index, line in enumerate(lines):
        match = DEF_RE.match(line)
        if match:
            starts.append((match.group(2), index, len(match.group(1))))
    result = []
    for position, (name, start, indent) in enumerate(starts):
        end = len(lines) - 1
        for later_name, later_start, later_indent in starts[position + 1:]:
            if later_indent <= indent:
                end = later_start - 1
                break
        result.append((name, start, end, indent))
    return result


def dedent(text, levels):
    """去掉 `levels` 层 4 空格缩进，保留相对缩进。"""
    strip = 4 * levels
    out = []
    for line in text.split("\n"):
        if line.strip() == "":
            out.append("")
        elif line.startswith(" " * strip):
            out.append(line[strip:])
        else:
            out.append(line.lstrip())
    return "\n".join(out)


def collect(source, wanted):
    """按源码顺序收集函数体，附带它们前面的注释块。

    内层函数（缩进 8 / 12）不能单独提取：外层函数的区间本来就包含它们，
    再单独切一份就会把外层函数的尾巴当成内层函数的正文，导致 return 跑到函数外。
    所以这里只提取最外层（缩进 4）的函数；TARGETS 里写的内层名字仅作校验。

    同名函数只认第一次出现（`rank` 在 order_views 里有一个内层定义，别处还有
    一个同名的，后者属于无关代码，取第一个才是制图链路里那个）。
    """
    lines = source.split("\n")
    functions = scan_functions(lines)
    by_name = {}
    for name, start, end, indent in functions:
        if name not in by_name:
            by_name[name] = (start, end, indent)
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise SystemExit("这些函数没找到：%s" % ", ".join(missing))

    # 只保留缩进 4 的（main() 的直接子函数）；内层由外层带出
    outer = {name for name in wanted if by_name[name][2] == 4}
    nested = {name for name in wanted if by_name[name][2] > 4}
    covered = set()
    for name in outer:
        start, end, _indent = by_name[name]
        for inner in nested:
            inner_start = by_name[inner][0]
            if start < inner_start <= end:
                covered.add(inner)
    orphans = nested - covered
    if orphans:
        raise SystemExit("这些内层函数不在任何被提取的外层函数里：%s" % ", ".join(sorted(orphans)))

    ordered = sorted(outer, key=lambda name: by_name[name][0])
    chunks = []
    for name in ordered:
        start, end, indent = by_name[name]
        # 往上吸收紧邻的注释块（连续以 # 开头、且不比函数本身更深的行）
        head = start
        while head > 0:
            previous = lines[head - 1]
            if previous.strip().startswith("#") and len(previous) - len(previous.lstrip()) >= indent:
                head -= 1
            else:
                break
        body = "\n".join(lines[head:end + 1]).rstrip()
        chunks.append((name, dedent(body, indent // 4)))
    return chunks


HEADER = '''"""%(title)s

从 dsh-nx 的 nxworker.py 提取，去掉 dsh 依赖，可直接在 NX 的 batch 会话里跑。

所有函数依赖注入的运行时对象（见 nxdraft.runtime.Runtime），不再依赖闭包变量：
    rt.NX       NXOpen 模块
    rt.s        NXOpen.Session.GetSession()
    rt.state    可变字典，rt.state["part"] 是当前部件
    rt.out(*a)  日志
"""

'''


def write_module(path, title, chunks, imports=""):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(HEADER % {"title": title})
        if imports:
            handle.write(imports)
            handle.write("\n")
        for name, body in chunks:
            handle.write("\n")
            handle.write(body)
            handle.write("\n\n")
    return len(chunks)


def main():
    if len(sys.argv) < 3:
        raise SystemExit("用法: python extract.py <nxworker.py> <输出目录>")
    source_path = sys.argv[1]
    out_dir = sys.argv[2]
    with io.open(source_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    chunks = collect(source, TARGETS)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    target = os.path.join(out_dir, "_extracted.py")
    count = write_module(target, "制图函数（自动提取）", chunks)
    print("提取 %d 个函数 -> %s" % (count, target))
    print("行数: %d" % len(io.open(target, encoding="utf-8").read().split("\n")))


if __name__ == "__main__":
    main()
