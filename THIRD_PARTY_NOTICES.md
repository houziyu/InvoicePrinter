# 第三方组件与许可声明

本项目使用以下第三方软件。各组件的版权归原作者所有，项目自身的 AGPL-3.0-only 授权不改变第三方组件的独立许可条件。

以下版本对应当前 [requirements.txt](requirements.txt)。许可原文保存于 [licenses/](licenses/README.md)，主要从当前安装的发行包中原样复制；来源及具体文件见目录索引。

## 直接依赖

| 组件 | 版本 | 用途 | 许可与原文 |
| --- | --- | --- | --- |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | 1.28.2 | PDF 读取、文字提取、渲染与排版 | AGPL v3 / Artifex 商业双许可；本项目采用 AGPL 路径。[发行包声明](licenses/PyMuPDF/COPYING)、[AGPL 全文](LICENSE) |
| [MuPDF](https://github.com/ArtifexSoftware/mupdf) | 1.28.2 | PyMuPDF 的底层 PDF 引擎 | AGPL v3 / 商业双许可。[许可全文](licenses/MuPDF/COPYING) |
| [Pillow](https://github.com/python-pillow/Pillow) | 12.3.0 | 缩略图、预览图像与打印图像 | MIT-CMU；二进制依赖另有条款，已包含在[发行包完整许可文件](licenses/Pillow/LICENSE)中 |
| [pywin32](https://github.com/mhammond/pywin32) | 312 | Windows 打印机与 GDI 接口 | 包元数据标注 PSF；Win32 绑定附有 BSD 风格条款，其他组件依各自文件。[Win32 许可](licenses/pywin32/win32/License.txt)、[完整目录](licenses/pywin32/) |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) | 0.6.3 | Tkinter 文件拖放封装 | [MIT](licenses/tkinterdnd2/LICENSE) |
| [tkdnd](https://github.com/petasis/tkdnd) | 随 tkinterdnd2 发行包提供 | 原生拖放扩展 | Tcl 风格许可，保留 George Petasis 等版权与声明。[许可头原文](licenses/tkdnd/license-header.txt) |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | 6.22.3 | 构建单文件 EXE | GPL-2.0-or-later，附 bootloader 例外；运行时钩子等适用 Apache-2.0，详见[完整许可文件](licenses/pyinstaller/COPYING.txt) |

## Python 与图形运行时

EXE 通常还包含构建环境中的 Python、Tcl / Tk，以及被依赖库打包的原生组件。

- **Python**：采用 PSF 及历史许可，见 [Python 发行包完整许可](licenses/Python/LICENSE.txt)。当前许可副本来自 Python 3.13.12。
- **Tcl**：采用其宽松许可，见 [Tcl 许可](licenses/Tcl/license.terms)。副本来自上游 `core-8-6-branch`。
- **Tk**：采用其宽松许可，见 [Tk 许可](licenses/Tk/license.terms)。副本来自当前 Python 安装的 Tk 8.6。

`licenses/pywin32/` 保留了该 wheel 提供的许可集合，其中部分组件可能不被本程序实际导入。Pillow 的完整许可文件也包括该 wheel 自带图像库的声明。最终 EXE 包含的组件取决于构建环境与 PyInstaller 收集结果；更新依赖或发布不同架构时，应同步核对并更新许可材料。

## 对应源码与发布

项目自身的源码和构建脚本随仓库提供。发布 EXE 时，应在同一 Release 中提供匹配标签的源码入口，并按适用条款提供所包含的受 copyleft 约束组件及其修改的完整对应源码。

上游源码入口：

- [PyMuPDF 1.28.2 源码](https://github.com/pymupdf/PyMuPDF/tree/1.28.2)；也可在 [PyPI 对应版本](https://pypi.org/project/PyMuPDF/1.28.2/#files)获取源码发行包。
- [MuPDF 1.28.2 源码](https://github.com/ArtifexSoftware/mupdf/tree/1.28.2)；取得源码时应同时取得该版本构建所需的子模块和第三方源码。
- [Pillow 源码](https://github.com/python-pillow/Pillow)、[pywin32 源码](https://github.com/mhammond/pywin32)、[tkinterdnd2 源码](https://github.com/Eliav2/tkinterdnd2)、[tkdnd 源码](https://github.com/petasis/tkdnd)、[PyInstaller 源码](https://github.com/pyinstaller/pyinstaller)。
- [CPython 3.13.12 源码](https://github.com/python/cpython/tree/v3.13.12)、[Tcl 源码](https://github.com/tcltk/tcl)、[Tk 源码](https://github.com/tcltk/tk)。

发布者应保存实际构建使用的精确版本、源码、修改和构建材料，保证源码下载入口与发布的二进制相符且持续可用。仅提供项目自己的 Python 文件或可变的上游主分支链接，不能完整履行适用的对应源码义务。

分发时请随包附带 `LICENSE`、本文件及 `licenses/`，保留依赖要求的版权、免责声明和相关元数据。PyMuPDF 的授权选项及要求见 [官方许可说明](https://pymupdf.io/licensing)。
