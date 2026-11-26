"""制图选项：中文视图名、图号规则、尺寸控制。

单独一个模块，因为 GUI 和 CLI 都要用，而且这些是「业务约定」不是 NX 调用。
"""

# 视图中文名 <-> NX 建模视图名
VIEW_NAMES = [
    ("top", "俯视图", "TOP"),
    ("front", "主视图", "FRONT"),
    ("right", "右视图", "RIGHT"),
    ("left", "左视图", "LEFT"),
    ("bottom", "仰视图", "BOTTOM"),
    ("back", "后视图", "BACK"),
    ("isometric", "轴测图", "ISOMETRIC"),
    ("trimetric", "正二测图", "TRIMETRIC"),
]

# 允许的别名（用户可能写中文、英文或 NX 原名）
VIEW_ALIASES = {}
for key, chinese, nx_name in VIEW_NAMES:
    for alias in (key, chinese, nx_name, nx_name.lower(), key.capitalize()):
        VIEW_ALIASES[alias.lower()] = key

# 视图数量下拉的选项
VIEW_COUNTS = [1, 2, 3, 4]


def normalize_view(name):
    """把一个视图名（中文/英文/NX 原名）归一化成 api 里的 key。

    认不出来时返回 None —— 让调用方报错，而不是悄悄丢掉用户的选择。
    """
    if not name:
        return None
    text = str(name).strip()
    if not text:
        return None
    return VIEW_ALIASES.get(text.lower())


def views_to_flags(names):
    """['俯视图', 'Front'] -> {'top': True, 'front': True}"""
    flags = {}
    unknown = []
    for name in names or []:
        key = normalize_view(name)
        if key is None:
            unknown.append(name)
        else:
            flags[key] = True
    return flags, unknown


def view_label(key):
    """key -> 中文名，给界面显示用。"""
    for candidate, chinese, _nx in VIEW_NAMES:
        if candidate == key:
            return chinese
    return key


def drawing_number_rule(index, assembly=False, prefix="", start=1, digits=0):
    """图号：装配图固定 0，零件按顺序 +1。

    index 是零件在装配顺序里的位置（从 0 开始），assembly 为 True 时返回装配图号。
    digits > 0 时补零到该位数（如 digits=3 -> 001、002）。
    """
    if assembly:
        number = 0
    else:
        number = int(start) + int(index)
    text = str(number)
    if digits and digits > 0:
        text = text.zfill(int(digits))
    return (prefix or "") + text


def sheet_size_for(kind, requested=None):
    """默认幅面：零件图 A4，装配图 A3（装配图要放明细表）。"""
    if requested:
        return str(requested).upper()
    return "A3" if kind == "assembly" else "A4"
