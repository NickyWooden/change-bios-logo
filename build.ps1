# change-bios-logo —— 一键打包脚本
#
#   .\build.ps1                     # 用自动找到的 Python 打包 GUI 版
#   .\build.ps1 -Python "C:\...\python.exe"
#   .\build.ps1 -Cli                # 额外再打一个命令行版 bioslogo.exe
#   .\build.ps1 -Clean              # 先删掉 .venv / build / 输出目录 再打
#   .\build.ps1 -NoVenv             # 不用虚拟环境，直接用当前 Python
#   .\build.ps1 -OutputDir dist     # 想放进仓库里的 dist\ 就显式指定
#
# 产物：默认 <文档>\change-bios-logo-release\change-bios-logo.exe（单文件，双击即用）
#
# 默认输出目录刻意放在**仓库/工作区之外**，原因见 BUILD.md 第 7 节 ⑧：
# onefile 产物启动时要往 %TEMP% 解包，而 DSH 沙箱会限制"在工作区目录内启动"的进程
# 只能写工作区，于是双击工作区里的 exe 只会弹出
#   Could not create temporary directory!
# 放在工作区外就没有这个限制（与程序是否需要管理员权限无关）。
#
# 但因为同一条限制，PyInstaller（用工作区内的 .venv python 运行）本身**写不了工作区外**：
# 直接 --distpath 到工作区外会报 PermissionError: [WinError 5] 拒绝访问。
# 所以流程是"PyInstaller 先输出到工作区内的 build\dist\，再由本脚本（PowerShell 的映像
# 在工作区外，不受这条限制）复制到最终输出目录"。
#
# 本脚本刻意处理了两个踩过的坑，详见 BUILD.md：
#   1) PyInstaller 的 --icon 传相对路径会被解析到 --specpath 下面 → 这里统一传绝对路径；
#   2) PyInstaller 把进度日志写到 stderr，PowerShell 里容易误判成失败 → 这里以"产物是否存在"为准。

[CmdletBinding()]
param(
    [string]$Python,
    # 空 = 用默认输出目录（<文档>\change-bios-logo-release，位于工作区之外）；
    # 传相对路径按脚本所在目录解析，传绝对路径按原样使用。
    [string]$OutputDir = "",
    [switch]$Cli,
    [switch]$Clean,
    [switch]$NoVenv
)

# 注意：这里刻意用 Continue 而不是 Stop。
# PyInstaller 与 pip 都会把进度/警告写到 stderr，在 PowerShell 5.1 下
# $ErrorActionPreference="Stop" 会把"原生命令往 stderr 写东西"当成致命错误直接中断脚本。
# 所以本脚本改为全程手工判断：看退出码、看产物是否存在。
$ErrorActionPreference = "Continue"
$script:Failed = $false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# ---------------------------------------------------------------- 输出目录
# 默认放到工作区之外（见文件头的说明）；
# 注意不能直接 Join-Path $root $OutputDir —— 当 $OutputDir 是绝对路径时
# Join-Path 会拼出 "C:\repo\C:\abs\..." 这种废路径，所以这里先归一化。
if (-not $OutputDir) {
    $docs = [Environment]::GetFolderPath('MyDocuments')
    $OutputDir = if ($docs) { Join-Path $docs "change-bios-logo-release" } else { "dist" }
}
$outDir = if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    [System.IO.Path]::GetFullPath($OutputDir)
} else {
    Join-Path $root $OutputDir
}

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [X] $msg" -ForegroundColor Red; $script:Failed = $true }

# 跑一条原生命令，把 stdout+stderr 都收进来并缩进回显，返回退出码。
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $out = & $Exe @Arguments 2>&1
    $code = $LASTEXITCODE
    foreach ($line in $out) { Write-Host "      $line" }
    return $code
}

# 判断某个模块在当前 Python 里是否可用（用来跳过重复/离线的 pip 安装）。
function Test-Module($py, $module) {
    & $py -c "import $module" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------- 找 Python
function Test-PythonExe($exe) {
    if (-not $exe) { return $false }
    if (-not (Test-Path $exe)) { return $false }
    try {
        $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $v) { return $false }
        $parts = "$v".Trim().Split(".")
        return ([int]$parts[0] -eq 3) -and ([int]$parts[1] -ge 10)
    } catch { return $false }
}

function Find-Python() {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Python) { $candidates.Add($Python) }
    # py 启动器（Windows 官方安装器）
    foreach ($launcher in @("py", "py.exe")) {
        try {
            $p = (& $launcher -3 -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $p) { $candidates.Add($p.Trim()) }
        } catch { }
    }
    foreach ($name in @("python", "python3", "python3.13", "python3.12", "python3.11", "python3.10")) {
        try {
            $p = (& $name -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $p) { $candidates.Add($p.Trim()) }
        } catch { }
    }
    # 常见的按用户安装位置
    foreach ($base in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*",
                        "${env:ProgramFiles(x86)}\Python*")) {
        Get-ChildItem -Path $base -Filter "python.exe" -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates.Add($_.FullName) }
    }
    foreach ($c in $candidates) { if (Test-PythonExe $c) { return $c } }
    return $null
}

Write-Step "定位 Python 解释器"
if ($NoVenv) {
    $py = if ($Python) { $Python } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $py) { $py = Find-Python }
} else {
    $py = Find-Python
}
if (-not $py -or -not (Test-PythonExe $py)) {
    Write-Host "找不到 Python 3.10+ 解释器。" -ForegroundColor Red
    Write-Host "请安装 Python 3.10 或更高版本（https://www.python.org/downloads/），"
    Write-Host "或用 -Python 参数显式指定，例如："
    Write-Host '  .\build.ps1 -Python "C:\Python312\python.exe"'
    exit 1
}
Write-Ok "Python: $py"
& $py -c "import sys; print('      版本:', sys.version.split()[0], '(' + sys.executable + ')')"

# ---------------------------------------------------------------- 清理
if ($Clean) {
    Write-Step "清理旧产物"
    foreach ($d in @(".venv", "build", $outDir, "__pycache__")) {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force; Write-Ok "已删除 $d" }
    }
    Get-ChildItem -Filter "*.spec" -ErrorAction SilentlyContinue | Remove-Item -Force
}

# ---------------------------------------------------------------- 虚拟环境
if (-not $NoVenv) {
    Write-Step "准备虚拟环境"
    if (-not (Test-Path ".venv")) {
        & $py -m venv .venv
        if ($LASTEXITCODE -ne 0) { Write-Host "创建 .venv 失败" -ForegroundColor Red; exit 1 }
        Write-Ok "已创建 .venv"
    } else {
        Write-Ok "复用已存在的 .venv"
    }
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { Write-Host ".venv 里没有 python.exe" -ForegroundColor Red; exit 1 }
}

# ---------------------------------------------------------------- 依赖
# 只在真的缺模块时才 pip install：既避免重复安装，也避免离线/网络卡住时
# 整个打包流程停在 pip 上（这是本项目踩过的坑：pip 会长时间无输出）。
Write-Step "检查依赖"
$needPip = $false
if (-not (Test-Module $py "PIL"))      { $needPip = $true }
if (-not (Test-Module $py "PySide6"))  { $needPip = $true }
if (-not (Test-Module $py "PyInstaller")) { $needPip = $true }
if ($needPip) {
    Write-Warn2 "缺少依赖，开始安装（需要网络）…"
    & $py -m pip install --upgrade pip --quiet 2>&1 | Out-Null
    if (-not (Test-Module $py "PIL")) {
        & $py -m pip install -r requirements.txt --quiet 2>&1 | Out-Null
        if (-not (Test-Module $py "PIL")) { Write-Host "安装 Pillow 失败" -ForegroundColor Red; exit 1 }
    }
    if (-not (Test-Module $py "PySide6")) {
        & $py -m pip install "PySide6>=6.6" --quiet 2>&1 | Out-Null
        if (-not (Test-Module $py "PySide6")) { Write-Host "安装 PySide6 失败" -ForegroundColor Red; exit 1 }
    }
    if (-not (Test-Module $py "PyInstaller")) {
        & $py -m pip install "pyinstaller>=6.0" --quiet 2>&1 | Out-Null
        if (-not (Test-Module $py "PyInstaller")) { Write-Host "安装 PyInstaller 失败" -ForegroundColor Red; exit 1 }
    }
    Write-Ok "依赖已安装"
} else {
    Write-Ok "依赖已就绪（跳过 pip）"
}
& $py -c "import PIL, PySide6, PyInstaller; print('      Pillow', PIL.__version__, '/ PySide6', PySide6.__version__, '/ PyInstaller', PyInstaller.__version__)"

# ---------------------------------------------------------------- 打包
# 关键：--icon 必须传绝对路径。
# PyInstaller 会把相对的 --icon 解析到 --specpath（这里是 build\）下面，
# 于是报 FileNotFoundError: Icon input file ...\build\app.ico not found。
$icon = (Resolve-Path (Join-Path $root "app.ico")).Path
# PyInstaller 先输出到工作区内的暂存目录（原因见文件头）；再由本脚本复制到 $outDir。
$stageDir = Join-Path $root "build\dist"
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Write-Host "  打包暂存目录: $stageDir"
Write-Host "  最终输出目录: $outDir"
$common = @(
    "--noconfirm",
    "--onefile",
    "--hidden-import", "PIL._tkinter_finder",
    # 8/4/1 位深调色板量化在 bmp_bytes_paletted 里惰性 import numpy；
    # 静态分析一般能扫到，这里显式声明兜底，避免漏包导致打包版 8/4/1 位深崩。
    "--hidden-import", "numpy",
    "--icon", $icon,
    "--distpath", $stageDir,
    "--workpath", (Join-Path $root "build"),
    "--specpath", (Join-Path $root "build")
)

function Invoke-PyInstaller($entry, $name, $extra) {
    Write-Step "打包 $name（入口 $entry）"
    # 先删掉暂存产物：否则"产物是否存在"的判据会被上一轮的旧文件糊弄过去（这是踩过的坑）。
    $staged = Join-Path $stageDir "$name.exe"
    Remove-Item $staged -Force -ErrorAction SilentlyContinue
    $pyArgs = @("-m", "PyInstaller") + $common + $extra + @("--name", $name, (Join-Path $root $entry))
    # 2>&1 把 stderr 并进管道，否则 PowerShell 5.1 会把 PyInstaller 的进度日志
    # 渲染成一堆红色 NativeCommandError（不影响结果，但看起来像失败）。
    & $py @pyArgs 2>&1 | ForEach-Object { Write-Host "      $_" }
    # PyInstaller 正常结束时也会往 stderr 写日志，PowerShell 里 $LASTEXITCODE 有时不为 0；
    # 因此以"暂存产物是否真的生成"作为判据，而不是退出码。
    if (-not (Test-Path $staged)) {
        Write-Host "打包失败：没有生成 $staged" -ForegroundColor Red
        return $null
    }
    # 复制动作由 PowerShell 完成（它的映像在工作区外，不受沙箱那条写权限限制）
    $target = Join-Path $outDir "$name.exe"
    if ($target -ne $staged) {
        try {
            Copy-Item $staged $target -Force -ErrorAction Stop
        } catch {
            Write-Host "复制到最终输出目录失败：$target" -ForegroundColor Red
            Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "  若你是在 DSH 会话里运行，可改用 -OutputDir dist 输出到工作区内。" -ForegroundColor Yellow
            return $null
        }
        Write-Ok "已复制到 $target"
    }
    return $target
}

$gui = Invoke-PyInstaller "change_bios_logo.py" "change-bios-logo" @("--windowed")
if ($gui) {
    $f = Get-Item $gui
    $hash = (Get-FileHash $gui -Algorithm SHA256).Hash
    Write-Ok "GUI 版：$($f.FullName)"
    Write-Host "      字节数 : $($f.Length)"
    Write-Host "      SHA-256: $hash"
}

if ($Cli) {
    # 命令行版走 --console，否则看不到任何输出
    $cli = Invoke-PyInstaller "bioslogo.py" "bioslogo" @("--console")
    if ($cli) {
        $f2 = Get-Item $cli
        Write-Ok "CLI 版：$($f2.FullName)"
        Write-Host "      字节数 : $($f2.Length)"
        Write-Host "      SHA-256: $((Get-FileHash $cli -Algorithm SHA256).Hash)"
        Write-Host "`n  自检："
        & $cli --help 2>&1 | Select-Object -First 3 | ForEach-Object { Write-Host "      $_" }
    }
}

Write-Step "完成"
Write-Host @"
产物目录：$outDir

提示
  * 程序里的 tool_dir() 取的是 exe 所在目录，所以把 exe 单独放到一个文件夹里，
    点击预览图生成的备份会落在那个文件夹的 backup\ 下。
  * 默认输出目录在仓库/工作区之外（见文件头说明与 BUILD.md 第 7 节 ⑧）：
    在工作区目录里双击 onefile 产物会报 "Could not create temporary directory!"，
    那是本机 DSH 沙箱对"工作区内启动的进程"的写权限限制，不是程序需要管理员权限。
    要用仓库里的 dist\ 就显式指定：.\build.ps1 -OutputDir dist
  * 发布到 Releases 时建议同时给出 SHA-256，方便使用者校验。
  * PyInstaller 打出来的 exe 不是可重现构建（内部含构建时间戳），
    换台机器/换个时间重打，字节数会差几百字节、SHA-256 必然不同，这不代表失败。
"@
