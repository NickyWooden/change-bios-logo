# 打包说明（BUILD.md）

把 `change-bios-logo` 打包成可分发的可执行产物：**Windows 单文件 exe**（双击即用、不需要装 Python），
或 **Debian / Ubuntu 的 `.deb`**（`dpkg -i` 安装、自带 venv、自包含）。

运行依赖见 [requirements.txt](requirements.txt)：`Pillow`（图片处理）、`numpy`（8/4/1 位调色板量化）、
`PySide6`（GUI，Qt 全家桶）；压缩用标准库 `lzma`。两种产物都会把这些依赖**打包进去**
（exe 由 PyInstaller 收进单文件；deb 装进自包含的 venv），终端用户无需自己装 Python 或 pip 包。

---

## 目录

* [1. 环境要求](#1-环境要求)
* [2. 一键打包（推荐）](#2-一键打包推荐)
* [3. 手动分步打包](#3-手动分步打包)
* [4. PyInstaller 参数说明](#4-pyinstaller-参数说明)
* [5. 打包命令行版（可选）](#5-打包命令行版可选)
* [6. 产物校验](#6-产物校验)
* [7. 已知的坑](#7-已知的坑)
* [8. 发布到 GitHub Releases](#8-发布到-github-releases)
* [9. Debian / Ubuntu 打包（.deb）](#9-debian--ubuntu-打包deb)
* [10. 参考版本与产物指纹](#10-参考版本与产物指纹)

---

## 1. 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | **Windows 10 / 11**（打 exe）或 **Debian 12+ / Ubuntu 22.04+**（打 deb，amd64）。文件对话框用 Qt 非原生实现、跨平台；`os.startfile` 的「打开工具目录」按钮仅 Windows 有效（Linux 下为占位） |
| Python | **3.10 或更高**（用到了 `Image.Quantize` / `Image.Dither` 等较新的枚举）；deb 的 venv 与解释器版本绑定，建议 3.14 |
| Pillow | `>=10.1`（开发与验证时使用 12.3.0） |
| PyInstaller | `>=6.0`（开发与验证时使用 6.22.3）；只在打 exe 时需要，deb 不需要 |
| 打包工具 | 打 exe：Windows PowerShell 5.1 或 PowerShell 7+；打 deb：`dpkg-deb`（dpkg 自带）+ `python3`，建议装 `uv` 加速 |

> 用虚拟环境是强烈建议的做法：PyInstaller 会把当前环境里"被导入到的"包一起收进 exe，
> 环境越干净，打出来的 exe 越小、越不容易把无关模块带进去。

---

## 2. 一键打包（推荐）

仓库根目录下执行：

```powershell
.\build.ps1
```

> 如果报 **`无法加载文件 ...\build.ps1，因为在此系统上禁止运行脚本`**，说明 PowerShell 的执行策略
> 不允许跑脚本。两种解法（任选其一）：
>
> ```powershell
> # 解法 A：只给这一个脚本放行，不改系统设置
> powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
>
> # 解法 B：给当前用户放开本地脚本（一次性设置，之后直接 .\build.ps1 即可）
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

脚本会依次做这些事：

1. 自动寻找 Python 3.10+（`py -3` 启动器 → `python` / `python3` → 常见的按用户安装目录）；
2. 建 `.venv` 虚拟环境（已存在就复用）；
3. 安装 `Pillow` 与 `PyInstaller`；
4. 用 PyInstaller 打单文件 GUI exe；
5. 打印产物的**字节数**与 **SHA-256**。

常用参数：

```powershell
.\build.ps1 -Clean                 # 先删掉 .venv / build / 输出目录 再重打
.\build.ps1 -Cli                   # 额外再打一个命令行版 bioslogo.exe
.\build.ps1 -NoVenv                # 不用虚拟环境，直接用当前 Python
.\build.ps1 -OutputDir dist        # 指定输出目录（相对路径按脚本所在目录解析）
.\build.ps1 -Python "C:\Python312\python.exe"   # 显式指定解释器
```

产物：**默认输出到工作区之外**的 `<文档>\change-bios-logo-release\change-bios-logo.exe`
（本机即 `C:\Users\rose\Documents\change-bios-logo-release\change-bios-logo.exe`）。

> 为什么默认不放在仓库里的 `dist\`：onefile 产物每次启动都要在 `%TEMP%` 下解包，
> 而 DSH 沙箱会限制"在工作区目录内启动的进程"只能写工作区，于是双击工作区里的 exe
> 只会弹出 `Could not create temporary directory!`（详见第 7 节 ⑧）。
> 想把产物放回仓库，显式写 `-OutputDir dist` 即可。

脚本还会先检查 `.venv` 里 `Pillow` / `PySide6` / `PyInstaller` 是否齐全，
**缺什么才装什么**（都齐了就直接跳过 `pip`，避免离线或网络慢时卡在安装步骤）。

---

## 3. 手动分步打包

等价于 `build.ps1` 做的事，想自己控制每一步时用：

```powershell
# 1) 虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) 依赖
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "pyinstaller>=6.0"

# 3) 打包（注意 --icon 用绝对路径，原因见第 7 节）
python -m PyInstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --name change-bios-logo `
  --icon "$((Resolve-Path .\app.ico).Path)" `
  --hidden-import PIL._tkinter_finder `
  --distpath dist `
  --workpath build `
  --specpath build `
  change_bios_logo.py
```

产物：`dist\change-bios-logo.exe`

不需要 `--add-data`：界面没有任何外部资源文件（图片全部由代码生成），`app.ico` 是**编译进 exe 的图标**，不是运行时资源。

---

## 4. PyInstaller 参数说明

| 参数 | 为什么这么写 |
| --- | --- |
| `--onefile` | 打成单个 exe，方便分发。启动时会先把自己解包到 `%TEMP%\_MEIxxxx`，因此**首次启动比 `--onedir` 慢一两秒** |
| `--windowed` | GUI 程序，不要弹控制台黑框。**反过来说：这个版本看不到任何 `print` 输出**，调试请改用源码运行或第 5 节的 `--console` 版 |
| `--name change-bios-logo` | 产物名 |
| `--icon <绝对路径>` | exe 图标。**必须传绝对路径**，详见第 7 节 |
| `--hidden-import PIL._tkinter_finder` | 历史遗留的安全声明：旧 tkinter 版里 Pillow 会动态导入这个模块，PyInstaller 的静态分析有时漏掉它；现在 GUI 已换 PySide6、代码里不再直接用它，但留着无害，留着以防 Pillow 内部某条路径仍会碰到 |
| `--distpath` / `--workpath` / `--specpath` | 把中间产物都收进 `build\`，仓库根目录只留源码 |

可选优化：

* **不要试图排除 `numpy`**。本项目确实用到了它：`bioslogo.py` 里的调色板路径
  （8 / 4 / 1 位）靠 `numpy` 做位打包与索引展开，`Pillow` 的部分插件也会引用它。
  排除能明显减小体积，但会让低位深功能在运行时报 `ModuleNotFoundError`，
  而这类问题**打包阶段完全看不出来**。
* `--exclude-module` 的正确用法是排除**确定用不到**的模块。本项目实际只用到 Pillow 的
  `Image` / `ImageDraw` / `ImageFont` 与标准库的 `lzma` / `numpy`，GUI 用 `PySide6`（Qt）。
  **每排除一个都要重新跑一遍完整功能验证**，否则很容易"打包成功、运行才崩"。
* `glass.py` 是**同目录下的本地模块**（`import glass`），PyInstaller 会自己跟着导入分析收进去，
  **不需要** `--hidden-import glass`，也**不需要** `--add-data`。
* **不要用 UPX**：UPX 压缩过的 exe 经常被杀毒软件误报，而本项目 exe 已经接近 30 MB，压这点体积不值得。
* 想要启动更快，可以把 `--onefile` 换成 `--onedir`，代价是分发时得给一个文件夹。

---

## 5. 打包命令行版（可选）

`bioslogo.py` 自带 CLI（`--list` / `--extract` / `--replace` …），可以单独打一个**控制台版**：

```powershell
python -m PyInstaller --noconfirm --onefile --console --name bioslogo `
  --icon "$((Resolve-Path .\app.ico).Path)" `
  --distpath dist --workpath build --specpath build bioslogo.py
```

或者直接 `.\build.ps1 -Cli`，两个 exe 一起出。

**必须是 `--console`**：GUI 版用的 `--windowed` 会把标准输出丢掉，CLI 版会变得"什么都不打印"。

---

## 6. 产物校验

打完包至少做这四步：

```powershell
# 1) 产物存在 + 记录指纹
Get-Item .\dist\change-bios-logo.exe | Select-Object Name, Length
Get-FileHash .\dist\change-bios-logo.exe -Algorithm SHA256

# 2) 窗口能起来（手动）——双击 exe，确认：
#    * 标题是「change-bios-logo — BIOS 开机 Logo 修改工具」
#    * 有四个按钮：① 载入 BIOS 文件 / ② 上传新 Logo / ③ Logo 替换 / 打开工具目录
#    * 底部日志第一行是「change-bios-logo 已启动」，
#      第二行「工具目录」指向 exe 所在目录（不是 %TEMP%）

# 3) CLI 版做一次端到端（会真写文件）
.\dist\bioslogo.exe --in <某个BIOS> --list
.\dist\bioslogo.exe --in <某个BIOS> --replace <新图> --out out.F44d

# 4) 验证"冻结环境"和源码跑出来的结果一致：
#    同一个 BIOS + 同一张图 + 同一组参数，两次输出的 BIOS 文件
#    SHA-256 必须完全相同（比对的是**改出来的 BIOS**，不是 exe）
Get-FileHash out.F44d -Algorithm SHA256
```

第 4 步很关键：PyInstaller 冻结后如果 `lzma` 或 Pillow 的子模块没被正确收进去，
程序**不会报错**，而是给出不一样的结果或直接崩——只有比对哈希才能发现。

> 注意比对的对象：**改出来的 BIOS 文件**是确定性的，同样的输入必须得到同样的 SHA-256；
> 而 **exe 本身不是可重现构建**（PE 头里带构建时间戳），换个时间重打哈希必然不同，
> 不要拿 exe 的哈希做这项校验。

`tool_dir()` 的行为也值得确认一次：冻结运行时它取 `Path(sys.executable).parent`，
所以**把 exe 单独放到一个文件夹**再运行，备份就会落在那个文件夹的 `backup\` 下。

---

## 7. 已知的坑

### ① `--icon` 传相对路径会报 `FileNotFoundError`

如果同时指定了 `--specpath build`，PyInstaller 会把**相对的** `--icon app.ico`
解析到 `build\app.ico` 下面，于是报：

```
FileNotFoundError: Icon input file C:\...\build\app.ico not found
```

**解法**：传绝对路径。

```powershell
--icon "$((Resolve-Path .\app.ico).Path)"
```

### ② PyInstaller 的日志走 stderr，容易被误判成失败

PyInstaller 把进度日志写到 **stderr**。在 PowerShell 里只要把输出接到管道
（例如 `... 2>&1 | Select-String ...`），`$LASTEXITCODE` 就可能不是 0，
看起来像"构建失败"，但日志里其实写着 `Build complete!`。

**解法**：不要拿退出码判断成败，**直接检查 `dist\change-bios-logo.exe` 是否存在**。
`build.ps1` 就是这么判断的。

### ③ 中文反馈在 GBK 控制台会崩

CLI 版会输出 `✓` 等符号。在代码页 936 的控制台里，`print` 这些字符会抛：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2713' in position 50: illegal multibyte sequence
```

**已经修掉了**：`bioslogo.py` 的 CLI 入口对 `stdout` / `stderr` 做了
`reconfigure(errors="replace")`，在 GBK 控制台下会把 `✓` 显示成 `?` 而不再崩。

如果你自己新加了带非 GBK 字符的输出，记得沿用这个做法。

### ④ `--windowed` 版本看不到任何输出

`--windowed` 会把 stdout / stderr 丢掉，出问题时没有任何报错信息。
排查时请改用 `python change_bios_logo.py` 直接跑源码，或者先用 `--console` 打一个临时版本。

### ⑤ 不同 Pillow / PyInstaller 版本打出来的 exe 不一样大

这是正常的（字节数、SHA-256 都会变），**功能不受影响**。
但如果你想复现某个已发布版本的指纹，必须用**完全相同的版本组合**。

### ⑥ 执行策略禁止运行脚本

```text
无法加载文件 C:\...\build.ps1，因为在此系统上禁止运行脚本。
有关详细信息，请参阅 https:/go.microsoft.com/fwlink/?LinkID=135170 中的 about_Execution_Policies。
```

Windows 客户端默认的执行策略是 `Restricted`，不允许执行任何 `.ps1`。**解法见第 2 节**：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1     # 只给这一个脚本放行
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned                  # 或给当前用户长期放开
```

### ⑦ `build.ps1` 必须存成「UTF-8 带 BOM」，否则中文注释会把脚本搞坏

这个坑很隐蔽：**Windows PowerShell 5.1 在没有 BOM 时会按系统 ANSI 代码页（简体中文下是 GBK）解码 `.ps1`**，
于是脚本里的中文注释和提示会变成乱码，**并且乱码里出现的字符会破坏语法**，报出一大串莫名其妙的错误：

```text
Unexpected token '}' in expression or statement.
Missing expression after ','.
An expression was expected after '('.  Not all parse errors were reported.
```

而同一份文件在 **PowerShell 7** 下能正常跑（7 默认按 UTF-8 读），所以很容易变成"我这儿好好的"。

**解法**：把 `build.ps1` 保存为 **UTF-8 with BOM**（仓库里已经是这个格式）。
如果自己新写了带中文的 `.ps1`，存盘时请勾选"UTF-8 带签名"，或重新编码一次：

```powershell
$p = (Resolve-Path .\build.ps1).Path
$text = [System.IO.File]::ReadAllText($p, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText($p, $text, (New-Object System.Text.UTF8Encoding($true)))   # $true = 写入 BOM
```

检查 BOM 是否存在：

```powershell
$b = [System.IO.File]::ReadAllBytes((Resolve-Path .\build.ps1).Path)
"{0:X2} {1:X2} {2:X2}" -f $b[0], $b[1], $b[2]     # 应输出 EF BB BF
```

顺带一个不用真的执行就能验语法的办法：

```powershell
$err = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path .\build.ps1).Path, [ref]$null, [ref]$err)
if ($err.Count) { $err | ForEach-Object Message } else { "语法 OK" }
```

---

### ⑧ 在工作区内双击 exe 会报 `Could not create temporary directory!`

**现象**：双击 `dist\change-bios-logo.exe` 后不出现界面，只弹一个标题为 `Error` 的对话框：

```text
Could not create temporary directory!
```

而右键「以管理员身份运行」却能正常打开——看起来像"这个程序必须要管理员权限"。

**真正原因**：程序本身不需要管理员权限，是**这台开发机上 DSH 的沙箱按"可执行文件所在位置"限制写权限**：

1. PyInstaller 的 `--onefile` 产物启动时，bootloader 会先在 `%TEMP%` 下创建 `_MEIxxxxx` 目录并把运行时解包进去；
2. 只要 **exe 文件位于 DSH 工作区目录内**，该进程就只能在**工作区内**写文件：往 `%TEMP%` 创建目录一律返回"拒绝访问"。用同一个探针在两处解释器里实测：

   | 解释器（exe 所在位置） | `CreateDirectoryW(%TEMP%\_MEI<pid>)` | 工作区内 |
   | --- | --- | --- |
   | 工作区内 `.venv\Scripts\python.exe` | `False` err=**5**（拒绝访问） | `True` |
   | 工作区外系统 Python | `True` err=0 | `True` |

3. 于是 bootloader 报 `Could not create temporary directory!`；以管理员身份运行时进程不受沙箱限制，所以能打开。
4. 与程序本身无关：**同一份 exe 复制到工作区外，普通双击即可打开，全程不提权**。实测用计划任务（干净、未提升的令牌）启动 `C:\Users\rose\Documents\change-bios-logo-release\change-bios-logo.exe`：界面正常出现，`%TEMP%` 下正常生成 `_MEI` 目录，运行日志第一行「工具目录」指向该文件夹。

**做法**：

* **不要在工作区目录内直接运行打包产物**：复制到工作区外（例如 `C:\Users\rose\Documents\change-bios-logo-release\`）再双击；
* 打包默认就输出到工作区外：`.\build.ps1` 会把产物放到 `<文档>\change-bios-logo-release\`（见第 2 节）；
  想把产物放回仓库里的 `dist\` 才需要显式指定 `-OutputDir dist`；
* 这条沙箱限制还有另一面：**PyInstaller 自己（用工作区内的 `.venv` python 运行）也写不了工作区外**，
  直接 `--distpath` 到工作区外会报 `PermissionError: [WinError 5] 拒绝访问`。所以 `build.ps1`
  的流程是"先让 PyInstaller 输出到工作区内的 `build\dist\`，再由 PowerShell 复制到最终目录"
  （PowerShell 的映像在工作区外，不受这条限制）；

* **不要用 UAC 清单**（`requireAdministrator`）去"解决"它：那会让**所有使用者**每次启动都弹 UAC 提权，而根因只存在于本机的工作区沙箱里，与分发给别人的 exe 无关；
* 首次双击若出现「打开文件 - 安全警告 / 无法验证发布者。你确定要运行此软件吗?」（未签名程序的常规确认框），点「运行(R)」即可，这不是权限问题；
* 想临时验证"是不是沙箱造成的"，把同一个 exe 分别放在工作区内、外各启动一次，看 `%TEMP%` 下有没有生成 `_MEI*` 目录即可：

  ```powershell
  Get-Process change-bios-logo | Select-Object Id, MainWindowTitle   # 界面版窗口标题是“change bios logo”
  Get-ChildItem $env:TEMP -Directory -Filter "_MEI*" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  ```

### ⑨ 打包版出问题先看 exe 同目录的 `change-bios-logo-error.log`

`--windowed` 的产物没有控制台，`sys.stdout` / `sys.stderr` 默认是 `None`：这种情况下任何槽函数抛出未捕获异常，连"打印 traceback"这一步都会失败，并把进程直接 abort——用户看到的就是"点一下直接闪退"，事件日志里是 `ucrtbase.dll` 的异常代码 `0xc0000409`。

`main()` 里现在做了两件事：

* `_ensure_std_streams()`：把 `stdout` / `stderr` 接到 **exe 同目录**的 `change-bios-logo-error.log`，异常至少留下可查痕迹；
* `_GuardedApplication.notify()`：捕获事件 / 槽里的未捕获异常，写日志并提示一次，**不再让整个进程退出**。

排查步骤：

1. 看 exe 同目录有没有 `change-bios-logo-error.log`，最后一段 traceback 就是出错点；
2. 需要更细的信息就用源码跑同一操作（有控制台，traceback 直接可见）：`.\.venv\Scripts\python.exe change_bios_logo.py`；
3. 修完重新打包：`.\build.ps1`（产物默认落在工作区外的 `<文档>\change-bios-logo-release\`）。

---

## 8. 发布到 GitHub Releases

```powershell
$tag = "v1.0.0"
.\build.ps1 -Clean
# 默认产物在 <文档>\change-bios-logo-release\（见第 2 节）
$exe = "$([Environment]::GetFolderPath('MyDocuments'))\change-bios-logo-release\change-bios-logo.exe"
"$((Get-FileHash $exe -Algorithm SHA256).Hash)  change-bios-logo.exe" |
    Out-File "$exe.sha256" -Encoding ascii

gh release create $tag $exe "$exe.sha256" `
    --title "change-bios-logo $tag" --notes "见 CHANGELOG"
```

> **不要把 exe 提交进 Git 仓库**：单文件 exe 接近 30 MB，而且每次重打包都是一个新的
> 二进制 blob，会让仓库迅速膨胀。仓库里只放源码（`.gitignore` 已经排除了 `dist/` 与 `*.exe`），
> 二进制走 Releases。

---

## 9. Debian / Ubuntu 打包（.deb）

除了 Windows exe，本项目也提供 **Debian / Ubuntu 的 `.deb`**（`deb/` 目录）。
与 exe 不同，deb **不用 PyInstaller**，而是把源码 + 一个**自包含的 venv** 装进
`/opt/change-bios-logo/`，终端用户 `dpkg -i` 即可，无需自己装 Python 或 pip 包。

### 9.1 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Debian 12+ / Ubuntu 22.04+（amd64） |
| dpkg-deb | dpkg 自带，Ubuntu/Debian 默认就有 |
| python3 | CPython 3.10+，建议 3.14（venv 的 ABI 与解释器版本绑定） |
| uv（可选） | uv 0.12+，建 venv 与装依赖都更快；没有也能用 `python3 -m venv` |

### 9.2 一键构建（推荐）

```bash
cd <项目根目录>/deb
./build-deb.sh
```

脚本依次做：复制源码到 `pkg/opt/change-bios-logo/` → 建 venv 并装依赖 →
用 `du` 重算 `Installed-Size` 写回 `DEBIAN/control` → `dpkg-deb --build --root-owner-group`
产出 `change-bios-logo_<版本>_<arch>.deb`。幂等，可重复跑。

### 9.3 产物结构

```
change-bios-logo_1.0.2-1_amd64.deb
└── (解包后)
    ├── DEBIAN/            # 元数据：control / postinst / postrm
    ├── opt/change-bios-logo/
    │   ├── change_bios_logo.py   # GUI 入口
    │   ├── bioslogo.py           # CLI 入口
    │   ├── glass.py              # 毛玻璃主题
    │   ├── requirements.txt
    │   ├── app.ico
    │   ├── docs/                 # 截图
    │   ├── backup/               # 运行时备份（0777）
    │   └── venv/                 # 自包含虚拟环境（PySide6 等，~272 MB）
    └── usr/
        ├── bin/change-bios-logo  # 入口脚本 → venv 里的 python
        ├── bin/bioslogo
        └── share/
            ├── applications/change-bios-logo.desktop   # 应用菜单
            └── icons/hicolor/256x256/apps/change-bios-logo.png
```

### 9.4 安装 / 卸载

```bash
# 安装（系统级依赖由 control 的 Depends 声明，缺了 dpkg 会提示）
sudo dpkg -i change-bios-logo_1.0.2-1_amd64.deb
# 或 apt（会自动从源补系统级依赖）
sudo apt install ./change-bios-logo_1.0.2-1_amd64.deb

# 启动
change-bios-logo        # GUI
bioslogo --list ...     # CLI

# 卸载（postrm 会清掉 .desktop 与图标缓存）
sudo dpkg -r change-bios-logo
```

> 安装后**重启应用**再验证：运行中的进程持有的是内存里的旧代码。
> 无 root 的环境（如容器）可 `dpkg-deb -x` 解包后直接跑
> `opt/change-bios-logo/venv/bin/python opt/change-bios-logo/change_bios_logo.py`。

### 9.5 与 exe 的差异

| | Windows exe | Debian / Ubuntu deb |
| --- | --- | --- |
| 打包方式 | PyInstaller 单文件 | 源码 + 自包含 venv |
| 安装 | 双击即用 | `dpkg -i` / `apt install` |
| 体积 | ~65 MB | ~66 MB（venv 占绝大部分） |
| 「打开工具目录」按钮 | 有效（`os.startfile`） | 占位（`os.startfile` 仅 Windows，Linux 下点一下只在日志里提示） |
| 应用菜单 / 图标 | 无（靠 exe 自身） | 有（.desktop + hicolor 图标） |

---

## 10. 参考版本与产物指纹

本文档中所有验证结论来自下面这套组合，可作为复现基准：

| 组件 | 版本 |
| --- | --- |
| Windows | 11 |
| Python | 3.12.10 |
| Pillow | 12.3.0 |
| PySide6 | 6.11.2 |
| PyInstaller | 6.22.3 |
| PowerShell | 7（Windows PowerShell 5.1 亦可用） |

用上面的组合、按第 3 节命令打包，GUI 版产物的参考指纹：

| 项目 | 值 |
| --- | --- |
| 文件名 | `change-bios-logo.exe` |
| 字节数 | 约 `67,974,000` |
| SHA-256 | `0F7F6BBF85566654D45BA2E582A5C04F666F68D1682F464F110CB3858F4D0011` |

> ⚠️ **exe 不是可重现构建**。PyInstaller 会把构建时间戳写进 PE 头，因此
> **换台机器、换个时间重新打包，字节数会差几百字节、SHA-256 必然不同**——这不代表失败。
> 上面这个指纹只用于说明"哪一次构建"，**不能当作校验标准**。
>
> 真正确定的是**容器大小量级**：界面已从 tkinter 换成 PySide6，所以装了 Pillow + numpy + PySide6
> 之后，单文件 exe 稳定在 **65 MB 上下**（PySide6/Qt 的 DLL 与插件占绝大部分）。
> 如果你打出来的只有几 MB、或还停在旧的 30 MB 上下，那多半是 PySide6 没被收进去。
>
> 想确认打得对不对，请走第 6 节的四步校验，而不是比对 exe 的哈希。

Debian / Ubuntu deb 的参考指纹（`deb/build-deb.sh` 构建，Debian / Ubuntu amd64，Python 3.14.4，PySide6 6.11.2）：

| 项目 | 值 |
| --- | --- |
| 文件名 | `change-bios-logo_1.0.2-1_amd64.deb`（由 `DEBIAN/control` 的 `Version` 决定） |
| 字节数 | `68,728,706` |
| SHA-256 | `04dd426fa3207aaaa3b30dc577e2081df95b4d19f95a6086f0575783de0dd1b7` |

> deb 的指纹**比 exe 稳定得多**：`--root-owner-group` 固定了属主，避免了 uid/gid 抖动；
> 只要源码、依赖版本、`dpkg-deb` 版本一致，重打出来的 `.deb` 字节数与 SHA-256 基本可复现。
> 但 venv 里装的是 PySide6 等二进制 wheel，换发行版 / 换 Python 小版本仍可能让 wheel 不同，
> 从而让指纹变化——所以它同样**只用于说明"哪一次构建"，不做强校验**。
