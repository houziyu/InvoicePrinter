# 简印 · 发票打印

面向 Windows 的本地 PDF / 图片发票排版与打印工具。将多张发票放在同一张纸上，先预览，再打印或导出 PDF。

基于 Python、Tkinter 和 PyMuPDF 开发，无需登录。程序在本地读取和处理 PDF、图片，不向在线服务上传发票。

## 下载成品

Windows 10 / 11（64 位）用户可以直接[下载 InvoicePrinter.exe](https://github.com/houziyu/InvoicePrinter/releases/latest/download/InvoicePrinter.exe)，双击运行，无需安装 Python。

完整压缩包、校验文件和历史版本见 [Releases 发布页面](https://github.com/houziyu/InvoicePrinter/releases)。程序首次启动需要解压运行组件，可能等待数秒。

本次版本为 **v1.2.0**，新增图片格式支持，详细变更见 [更新记录](CHANGELOG.md)。

## 功能

- **批量导入**：选择或拖入 PDF、PNG、JPG/JPEG、BMP、TIF/TIFF、WebP、GIF，也可拖入文件夹（读取第一层支持的文件）；多页 PDF / TIFF 按页加入列表，动画图片使用首帧，相同路径下的相同页自动去重。后台读取提供进度与取消，问题文件可查看全部记录。
- **图片排版**：图片可与 PDF 混合预览、拼版、打印和导出 PDF，保持宽高比及原始像素，自动校正 EXIF 方向，透明区域铺白底。
- **列表管理**：支持缩略图与紧凑列表，按导入顺序、文件名或金额排序，也可拖动缩略图或文件名调整顺序。“添加顺序”可恢复原始导入顺序。
- **勾选与撤销**：只输出已勾选发票；筛选不会改变勾选状态，“全选当前 / 取消当前”仅影响当前筛选结果。可单独或批量移除、清空，并撤销最近 20 次移除；原始文件保留。
- **排版预览**：支持翻页、缩放、适合整页 / 宽度、滚动与拖动查看，以及每页 1 张、上下 2 张、左右 2 张三种布局。点击已勾选发票可定位并高亮预览位置。
- **纸张设置**：支持 A4、A5、Letter，纵向或横向，0–50 mm 页边距及裁剪线。
- **多份输出**：按每张发票连续排版；例如 A、B 各 2 份，上下两张布局会生成 `A+A`、`B+B`。
- **金额核对**：汇总已选发票的已识别金额，自动识别结果标为待核对。支持筛选未识别 / 待核对发票，点击金额核对或更正（不修改原文件）；图片金额需手动填写。
- **打印与导出**：支持打印机刷新、驱动属性、打印队列和后台打印提交 / 导出。导出禁止覆盖清单中的源 PDF 或图片，并采用临时文件原子替换，避免失败时损坏已有目标文件。
- **设置记忆**：记住纸张、方向、版式、裁剪线、边距、打印机和紧凑列表偏好。重启后每张份数恢复为 1。

打印任务会请求与预览一致的纸张、方向及单面打印。份数已在排版中展开，打印机驱动份数固定为 1，避免重复输出。

## 运行环境

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11 |
| 源码运行 | 推荐 Python 3.13，包含 Tkinter / Tcl / Tk |
| 打印 | 已安装的 Windows 打印机及相应驱动 |
| EXE 运行 | 不需要单独安装 Python；程序架构应与目标系统匹配 |

当前开发环境为 Python 3.13。依赖版本见 [requirements.txt](requirements.txt)。直接打印依赖 Windows API，其他系统不在本项目的支持范围内。

## 从源码运行

在 PowerShell 中克隆仓库并进入项目目录：

```powershell
git clone https://github.com/houziyu/InvoicePrinter.git
cd InvoicePrinter
```

也可以下载源码 ZIP，解压后在项目目录打开 PowerShell。随后创建虚拟环境并运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe invoice_printer.py
```

上述命令直接使用虚拟环境中的 Python，无需激活环境。如果系统找不到 `python`，请先安装 Python 并配置 PATH。

## 使用方法

1. 启动程序，点击“添加 PDF / 图片”（`Ctrl+O`），或将 PDF、图片、文件夹拖入窗口。
2. 检查列表、金额和排序，取消勾选暂时不打印的页面；误移除可点击撤销或按 `Ctrl+Z`。
3. 设置纸张、方向、布局、页边距和每张份数（1～99），核对预计用纸页数。
4. 在预览中检查分页和内容大小，可拖动查看放大内容，`Ctrl+滚轮` 可缩放。
5. 选择打印机并确认提交，或点击导出 PDF 保存排版结果。提交成功仅表示进入 Windows 队列，实际出纸状态请查看打印队列或设备。

耗时任务可通过右下角取消。取消导入会保留已读入页面；取消导出会在文件替换前停止；取消打印提交会尝试终止尚未完成的任务，已被设备接收的页面仍可能出纸。

导入后请保持原 PDF 或图片路径可用，预览、导出和打印仍需读取源文件。

## 构建 EXE

在 Windows 上使用与目标系统架构匹配的 Python 构建：

```powershell
.\.venv\Scripts\Activate.ps1
.\build.ps1
```

脚本会安装依赖、生成图标，并通过 PyInstaller 打包，输出为 `dist/InvoicePrinter.exe`。

如果 PowerShell 阻止脚本运行，可直接使用虚拟环境中的 Python 执行构建步骤：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe make_icon.py
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean InvoicePrinter.spec
```

EXE 已包含运行组件。首次启动时，单文件程序需要解压组件，可能等待数秒。重新打包前请关闭正在运行的 EXE。

## 测试

安装依赖后运行现有测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖纸张 / 方向 / 版式 / 份数组合、金额识别与核对、勾选、恢复导入顺序、拖动排序、撤销、重复导入、文件夹与中文路径、取消任务、预览更新竞争、导出保护、设置持久化，以及打印驱动参数与失败清理。界面测试需要 Windows 桌面会话，可能短暂显示窗口；自动化打印测试使用模拟对象，不向真实打印机发送任务。实际打印效果仍需结合设备和驱动检查。

## 常见问题与限制

**扫描件为什么识别不到金额？**

金额提取依赖 PDF 中可复制的文字，未集成 OCR。图片和扫描件不会自动识别金额，可点击金额手动填写；特殊编码 PDF 也可能无法识别，规则可能误识别其他数字。汇总仅供核对，不应代替原票金额。

**加密 PDF 可以导入吗？**

需要密码才能打开的 PDF 会被跳过，目前没有密码输入功能。请先使用有权访问的未加密副本。

**打印结果与预览不一致怎么办？**

检查纸张、方向、驱动和打印机的可打印区域。打印机属性可设置纸盒、彩色或黑白等设备选项；本程序会在提交任务时设置纸张、方向、单面和驱动份数。不同驱动对这些设置的支持可能不同。

**数据保存在哪里？**

程序读取本地 PDF 和图片，预览和排版主要在内存中处理；导出文件保存到自行选择的位置。打印任务交给 Windows 打印队列，网络打印机可能通过局域网传输打印数据。移除列表项不会删除原文件；导出时会阻止覆盖当前清单里的源 PDF 和图片。

偏好设置保存到 `%LOCALAPPDATA%\InvoicePrinter\settings.json`，不保存发票路径、金额或清单。手工核对的金额、勾选状态和撤销记录只在本次会话中保留，退出前请完成所需导出。

## 项目结构

```text
InvoicePrinter/
├── invoice_printer.py       # 界面、PDF 排版、金额提取与打印
├── make_icon.py             # 图标生成脚本
├── app.ico                 # 应用图标
├── build.ps1               # Windows 构建脚本
├── InvoicePrinter.spec     # PyInstaller 构建配置
├── requirements.txt        # 固定版本依赖
├── tests/                  # 自动化测试
├── licenses/               # 第三方许可原文和来源说明
├── LICENSE                 # AGPL-3.0 协议全文
├── THIRD_PARTY_NOTICES.md   # 第三方组件声明
└── CONTRIBUTING.md         # 贡献指南
```

## 上传 GitHub 与发布

源码仓库应包含以上源码、测试和文档。`.gitignore` 已排除虚拟环境、缓存、`build/`、`dist/`、EXE 和本地 PDF；通过 GitHub 网页手动上传时，也请自行排除这些文件，网页上传不会替你应用本地忽略规则。

本项目仓库为 [houziyu/InvoicePrinter](https://github.com/houziyu/InvoicePrinter)。首次将本地目录上传到空仓库时，可在项目目录执行；如果发布到自己的 Fork，请替换仓库地址：

```powershell
git init
git add .
git status --short
git commit -m "Initial open-source release"
git branch -M main
git remote add origin https://github.com/houziyu/InvoicePrinter.git
git push -u origin main
```

提交前检查 `git status`，确保没有真实发票、个人信息或凭据。后续如需加入公开 PDF 示例，请使用脱敏或合成数据，并明确调整忽略规则。

EXE 建议通过 GitHub Releases 发布。每次发布应使用对应的 Git 标签标记源码，并将 EXE、`LICENSE`、`THIRD_PARTY_NOTICES.md` 和 `licenses/` 一起打包。发布说明中提供与该 EXE 匹配的完整对应源码下载入口；打包依赖及其源码获取信息见第三方声明。

## 贡献

欢迎通过 Issue 反馈问题或通过 Pull Request 提交改进。复现步骤、测试方法和隐私要求见 [贡献指南](CONTRIBUTING.md)。

## 开源协议

Copyright (c) 2026 InvoicePrinter contributors.

本项目原创代码、文档及图标采用 **GNU Affero General Public License v3.0 only**（SPDX：`AGPL-3.0-only`），协议全文见 [LICENSE](LICENSE)。软件按协议以“现状”提供，不提供担保。

项目使用的 PyMuPDF / MuPDF 提供 [AGPL 与商业双许可](https://pymupdf.io/licensing)。本仓库按 AGPL 开源路径发布。允许在遵守协议的前提下使用、修改和再分发，包括商业用途；分发修改版或二进制时应履行对应源码、许可和版权保留义务，修改版的远程网络交互还需遵守第 13 条。详细条件以协议原文为准。

第三方组件仍适用各自的许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
