# nxdraft

**Siemens NX 制图工具 · 独立版（含图形界面与 exe）**

从 [dsh-nx](https://github.com/aaz111/dsh-nx) 抽出来的制图部分，**不依赖 DeepSeek Harness**。
三种用法：双击 exe 开图形界面、命令行调、或起 JSON 命令队列给别的程序用。

在 NX batch 会话里出零件图、打原生尺寸、画 GB 图框、生成装配明细表、批量打印 PDF。

---

## 快速开始

### 方式一：双击 exe（图形界面）

下载 `nxdraft.exe`，双击即可 —— 会自动打开浏览器显示操作界面。
不需要装 Python，但**本机必须装 Siemens NX**。

```bash
nxdraft.exe gui                       # 打开图形界面
nxdraft.exe gui --out D:\drawings     # 指定产物目录
nxdraft.exe gui --port 9000           # 指定端口（默认自动挑空闲端口）
```

### 方式二：命令行

```bash
nxdraft.exe drawing "E:\parts\固定板.prt" --out D:\out --size A3 --with-pdf
```

### 方式三：pip 安装

```bash
pip install nxdraft
nxdraft gui
nxdraft drawing part.prt --out D:\out
```

---

## 一、能做什么

| 能力 | 说明 |
| --- | --- |
| **零件图** | 自动投影视图（按「能标全尺寸的最少数量」选，板件 2 个）、比例搜索、视图布局 |
| **原生尺寸标注** | 线性 + 直径，读数是 NX 真实测量值，改模型后跟着变；自动避让、跨视图去重 |
| **剖视图** | 剖切面按孔轴投票自动定位；竖直剖走 UF_DRAW |
| **GB 图框** | 真实图框 + 标题栏（曲线 + 文字），会随 PDF 打印 |
| **明细表** | 装配 BOM 汇总 + 图纸上的表格（真实表格线 + 每格文字） |
| **批量出图** | 装配所有零件逐件出图 + PDF，**带装配引用保护** |
| **PDF** | A0~A4 幅面，单页或多图纸页 |
| **图形界面** | 本地 Web 界面，填路径点按钮；实时看 NX 日志；产物列表一键打开 |

### 不做什么

- **不能建运动模型**（NX Motion 只读）
- **投影视图没有隐藏线**（batch 会话限制，要看内部用剖视）
- **局部放大图内容空白**（只有边界圈，batch 会话限制）
- **同心圆 Ø 引线在圆密集时会交叉**（如偏心轴、阀体，需出图后人工调整）

---

## 二、图形界面

双击 exe 或运行 `nxdraft gui` 后，浏览器打开 `http://127.0.0.1:<端口>/`：

```
┌─────────────────────────────────────────────────────┐
│ nxdraft   [就绪]              NX D:\software\NX      │
│                               产物 D:\out            │
├────────────────────────────────┬────────────────────┤
│ 出零件图                        │ 产物                │
│  零件文件 [_______________]     │  PDF 固定板.pdf 46KB│
│  视图 [自动(最少)▾]              │  PDF 偏心轴.pdf 51KB│
│    [俯视图][主视图][右视图]...   │                    │
│  幅面 [A3▾] 材料 [45] 图号 [__] │ [刷新] [打开目录]   │
│  尺寸标注 ☑线性 ☑直径            │                    │
│  [出图 + 标注 + PDF]            │                    │
├────────────────────────────────┤                    │
│ 批量出图                        │                    │
│  装配文件 [___________] [分析装配]│                    │
│  图纸顺序        12 个零件       │                    │
│  图号  零件名称      数量        │                    │
│  1     弹簧塞 ×2      2  [↑↓]   │                    │
│  2     阀体1          1  [↑↓]   │                    │
│  ...                            │                    │
│  图号规则 [前缀][起始][补零][A4] │                    │
│  材料 [____________]            │                    │
│  尺寸标注 ☑线性 ☑直径 ☑先出装配图│                    │
│  [批量出图] [明细表]             │                    │
├────────────────────────────────┤                    │
│ 运行日志                        │                    │
└────────────────────────────────┴────────────────────┘
```

界面特点：

- **视图两选一**：下拉选「自动 / 1~4 个」，或点选具体视图（中文名显示，后端自动映射）。
  选数量时选满即止，选「自动」时按零件决定最少视图。
- **产物目录可自定义**：右侧「产物目录」填路径点「应用」即时生效（绝对路径或相对当前目录都行），
  会先建目录并试写一个临时文件验证可写，不可写直接报错不切换。
- **图纸顺序可视化**：点「分析装配」读出零件清单（名称 + 数量 + 装配顺序），
  用 ↑↓ 调整顺序，**图号实时跟着变**。
- **图号规则**：装配图固定 0，零件按顺序递增，可加前缀、可补零。
- **尺寸分开控制**：零件图的线性/直径各自独立勾选。
- **实时日志**：NX 侧每条输出都流式显示，不会看着像卡死（一次出图 20~80 秒）
- **任务后台跑**：HTTP 请求立刻返回 job id，前端轮询进度，不卡浏览器
- **只监听 127.0.0.1**：每次启动生成一次性 token 注入页面，接口校验 token + Origin

---

## 三、命令行用法

> 全局参数（`--nx-home` / `--out` / `--timeout` / `--quiet`）写在子命令**前后都行**。

### 出零件图

```bash
# 出图 + 标注 + PDF 一次做完
nxdraft drawing "E:\parts\固定板.prt" --out D:\out --size A3 --with-pdf

# 指定视图（中文名或 NX 原名都行）
nxdraft drawing part.prt --views 俯视图,主视图 --material 45 --drawing-no DSP-001
nxdraft drawing part.prt --views Top,Front

# 只出视图，不标尺寸
nxdraft drawing part.prt --no-dimension

# 只标直径
nxdraft drawing part.prt --no-length --with-pdf
```

**可用视图名**：俯视图(Top) / 主视图(Front) / 右视图(Right) / 左视图(Left) /
仰视图(Bottom) / 后视图(Back) / 轴测图(Isometric) / 正二测图(Trimetric)。

⚠️ **出图、标注、PDF 必须在同一个 NX 会话里完成**（工具已自动这样处理）。
NX 进程退出后图纸就不在内存里了，分开调用会报 `the part has no drafting views yet`。

### 分析装配（排图纸顺序用）

```bash
nxdraft analyze "E:\asm\装配.prt"
# 输出零件名称、数量、装配顺序 —— 界面用它填顺序表
```

### 批量出图（装配）

```bash
# 全部零件，A4，图号 DSP-001 起、补零 3 位
nxdraft batch "E:\asm\装配.prt" --out D:\out --size A4 \
    --no-prefix DSP- --no-digits 3

# 自定义图纸顺序（不在列表里的排后面，保持装配原顺序）
nxdraft batch "E:\asm\装配.prt" --order 泵体,阀体,销轴

# 零件图不标直径；不先出装配图
nxdraft batch "E:\asm\装配.prt" --no-diameter --no-assembly-drawing

# 只出指定零件
nxdraft batch --paths "E:\p\a.prt" "E:\p\b.prt" --out D:\out
```

**图号规则**：装配图固定 **0**，零件按当前图纸顺序从 `--no-start`（默认 1）递增；
`--no-digits` 补零；`--no-prefix` 加前缀。默认先出装配图（带明细表，A3）。

### 装配明细表

```bash
nxdraft bom "E:\asm\装配.prt" --out D:\out --csv D:\out\bom.csv
```

---

## 四、JSON 命令队列

给别的程序调用。协议形状和 dsh-nx 一致。

```bash
nxdraft serve --req D:\q\req --resp D:\q\resp --out D:\out
```

投一个请求到 `req/<id>.json`：

```json
{
  "id": "abc12345",
  "cmd": "drawing",
  "args": { "path": "E:\\parts\\固定板.prt", "size": "A3" }
}
```

结果写到 `resp/abc12345.json`：

```json
{ "id": "abc12345", "ok": true, "result": { "sheet": { "views": [...] } }, "logs": [...], "ms": 23841 }
```

`cmd` 是 `nxdraft.api` 里的函数名：`drawing` / `dimension` / `pdf` / `section_view` /
`section_vertical` / `detail_view` / `frame` / `note` / `gdt` / `balloon` /
`surface_finish` / `centerline` / `view_scale` / `view_move` / `sheet_delete` /
`bom` / `bom_table` / `batch_drawings`。

`args` 是该函数的具名参数。带 `"path"` 时会先打开那个 `.prt`。

> **串行处理**：每个请求在独立的 NX batch 会话里跑，一次只处理一个。

---

## 五、Python 里直接用

```python
from nxdraft import Runtime
import nxdraft.api as api

rt = Runtime(out_dir=r"D:\out")
api.open_part(rt, r"E:\parts\固定板.prt")

api.drawing(rt, size="A3")            # 视图 + 布局 + GB 图框
api.dimension(rt)                     # 原生尺寸
api.pdf(rt, path=r"D:\out\固定板.pdf")

# 装配
api.open_part(rt, r"E:\asm\装配.prt")
api.bom_table(rt)                     # 图纸上画明细表
api.batch_drawings(rt, size="A4")     # 逐件出图 + PDF
```

⚠️ 这段代码**必须在 NX 的 batch 会话里跑**（`run_journal.exe`），普通 Python 里
`import NXOpen` 会失败。用 CLI / GUI / 队列则不用管这一层。

---

## 六、工作原理

```
nxdraft.exe / CLI（普通 Python）
    └─ 生成一个 journal 脚本到临时目录
       └─ run_journal.exe <journal>      ← NX 自己起进程、注入 Python 与 NXOpen
          └─ journal 里 import nxdraft，按顺序跑多个 api 函数
             └─ 用 @@NXDRAFT_RESULT@@ 前缀行把 JSON 结果传回 stdout
```

三个关键点：

- **一次调用 = 一个 NX 进程**。出图→标注→PDF 必须在一个 journal 里连续跑完，
  所以 `launcher.run_steps()` 支持一次传多步。
- **exe 模式下 NX 是另一个进程**，import 不到 exe 里打包的模块，所以 `build.spec`
  把整个 `nxdraft` 目录作为 datas 释放到 `_MEIPASS`，`launcher.package_root()`
  再把那个目录塞进 journal 的 `sys.path`。
- `nxdraft/_extracted.py` 里的 100 个函数是从 dsh-nx 的 `nxworker.py` 原样提取的
  （那个文件把 9000 行代码全塞在 `main()` 里，函数都是闭包）。去掉闭包后，
  `NX` / `s` / `state` / `out` / `display_part` 由 `runtime.Runtime.install()`
  注入成模块全局变量。

重新提取：`python extract.py <nxworker.py> nxdraft`（依赖有变动时用 `check_deps.py` 复查）。

---

## 七、自己打包 exe

```bash
pip install pyinstaller
python -m PyInstaller build.spec --clean --noconfirm
# 产物：dist/nxdraft.exe（约 10 MB）
```

`build.spec` 已配好：把 `nxdraft/*.py` 与 `nxdraft/web/index.html` 作为 datas 带进去，
关掉 UPX（避免杀软误报），保留控制台（NX 日志和报错要看）。

---

## 八、已知限制

| 限制 | 原因 | 替代 |
| --- | --- | --- |
| 投影视图无隐藏线 | batch 会话不生成隐藏边 | 用 `section_view` 剖视 |
| 局部放大图内容空白 | batch 无缩放渲染通道 | 用剖视图代替 |
| 同心圆 Ø 引线交叉 | NX 直径标注引线长度不可控 | 出图后人工调整 |
| 每个命令独立起 NX | NX 一个进程只能一个会话 | 用 `batch` 一次处理全部零件 |
| 单次调用 20~80 秒 | NX 启动 + 计算 | 界面有实时日志，不会看着像卡死 |
| exe 需本机装 NX | NX 是商业软件，不能随包分发 | 装 NX 即可 |

---

## 九、许可

MIT
