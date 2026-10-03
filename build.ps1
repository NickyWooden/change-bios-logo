# change-bios-logo —— 一键打包脚本
#
#   .\build.ps1                     # 用自动找到的 Python 打包 GUI 版
#   .\build.ps1 -Python "C:\...\python.exe"
#   .\build.ps1 -Cli                # 额外再打一个命令行版 bioslogo.exe
#   .\build.ps1 -Clean              # 先删掉 .venv / build / dist 再打
#   .\build.ps1 -NoVenv             # 不用虚拟环境，直接用当前 Python
#
# 产物：<OutputDir>\change-bios-logo.exe（单文件，双击即用）
#
# 本脚本刻意处理了两个踩过的坑，详见 BUILD.md：
#   1) PyInstaller 的 --icon 传相对路径会被解析到 --specpath 下面 → 这里统一传绝对路径；
#   2) PyInstaller 把进度日志写到 stderr，PowerShell 里容易误判成失败 → 这里以"产物是否存在"为准。

[CmdletBinding()]
param(
    [string]$Python,
    [string]$OutputDir = "dist",
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
    foreach ($d in @(".venv", "build", $OutputDir, "__pycache__")) {
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
Write-Step "安装依赖"
& $py -m pip install --upgrade pip --quiet 2>&1 | Out-Null
& $py -m pip install -r requirements.txt --quiet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "安装 Pillow 失败" -ForegroundColor Red; exit 1 }
& $py -m pip install "pyinstaller>=6.0" --quiet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "安装 PyInstaller 失败" -ForegroundColor Red; exit 1 }
& $py -c "import PIL, PyInstaller; print('      Pillow', PIL.__version__, '/ PyInstaller', PyInstaller.__version__)"
Write-Ok "依赖就绪"

# ---------------------------------------------------------------- 打包
# 关键：--icon 必须传绝对路径。
# PyInstaller 会把相对的 --icon 解析到 --specpath（这里是 build\）下面，
# 于是报 FileNotFoundError: Icon input file ...\build\app.ico not found。
$icon = (Resolve-Path (Join-Path $root "app.ico")).Path
$common = @(
    "--noconfirm",
    "--onefile",
    "--hidden-import", "PIL._tkinter_finder",
    "--icon", $icon,
    "--distpath", (Join-Path $root $OutputDir),
    "--workpath", (Join-Path $root "build"),
    "--specpath", (Join-Path $root "build")
)

function Invoke-PyInstaller($entry, $name, $extra) {
    Write-Step "打包 $name（入口 $entry）"
    $pyArgs = @("-m", "PyInstaller") + $common + $extra + @("--name", $name, (Join-Path $root $entry))
    # 2>&1 把 stderr 并进管道，否则 PowerShell 5.1 会把 PyInstaller 的进度日志
    # 渲染成一堆红色 NativeCommandError（不影响结果，但看起来像失败）。
    & $py @pyArgs 2>&1 | ForEach-Object { Write-Host "      $_" }
    # PyInstaller 正常结束时也会往 stderr 写日志，PowerShell 里 $LASTEXITCODE 有时不为 0；
    # 因此以"产物是否存在"作为唯一判据，而不是退出码。
    $exe = Join-Path $root (Join-Path $OutputDir "$name.exe")
    if (-not (Test-Path $exe)) {
        Write-Host "打包失败：没有生成 $exe" -ForegroundColor Red
        return $null
    }
    return $exe
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
产物目录：$root\$OutputDir

提示
  * 程序里的 tool_dir() 取的是 exe 所在目录，所以把 exe 单独放到一个文件夹里，
    点击预览图生成的备份会落在那个文件夹的 backup\ 下。
  * 发布到 Releases 时建议同时给出 SHA-256，方便使用者校验。
  * PyInstaller 打出来的 exe 不是可重现构建（内部含构建时间戳），
    换台机器/换个时间重打，字节数会差几百字节、SHA-256 必然不同，这不代表失败。
"@
