# 用 PyInstaller 把 PhotonMap 打成单文件 exe（dist\PhotonMap.exe）
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "找不到虚拟环境解释器：$python"
}

Push-Location $root
try {
    & $python -m pip install -q "pyinstaller>=6.22"
    if ($LASTEXITCODE -ne 0) { throw "安装 PyInstaller 失败" }

    & $python -m PyInstaller --noconfirm --clean "packaging\photonmap.spec"
    if ($LASTEXITCODE -ne 0) { throw "打包失败" }

    $exe = Join-Path $root "dist\PhotonMap.exe"
    if (-not (Test-Path $exe)) { throw "没有生成 $exe" }

    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "已生成：$exe（$mb MB）"
}
finally {
    Pop-Location
}
