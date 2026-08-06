<#
.SYNOPSIS
    Codex QQ Bridge 一键部署：检查 Python -> 装依赖 -> 生成 config.json -> 自检端口。
.NOTES
    NapCat 需要单独准备（本仓库不打包 NapCat 本体）。
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "==============================" -ForegroundColor Cyan
Write-Host "  Codex QQ Bridge - Installer " -ForegroundColor Cyan
Write-Host "==============================" -ForegroundColor Cyan

# 1. locate python
$py = $null
foreach ($cand in @("python", "py -3", "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe")) {
    try {
        $v = & $cand --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(9|1[0-9])") { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Host "[FAIL] 未找到 Python 3.9+。请先安装 Python 并勾选 Add to PATH。" -ForegroundColor Red
    exit 1
}
Write-Host "[1/4] Python OK: $py" -ForegroundColor Green

# 2. install deps
Write-Host "[2/4] 安装依赖 (websocket-client)..." -ForegroundColor Green
& $py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] 依赖安装失败" -ForegroundColor Red; exit 1 }

# 3. create config.json if missing
if (-not (Test-Path "$Root\config.json")) {
    Write-Host "[3/4] 生成 config.json，请填写你的 QQ 号..." -ForegroundColor Green
    Copy-Item "$Root\config.example.json" "$Root\config.json"
    Write-Host "      已创建 $Root\config.json，请编辑填写 owner_qq / bot_qq。" -ForegroundColor Yellow
    try { Start-Process notepad "$Root\config.json" } catch {}
    Read-Host "编辑完成后按回车继续"
} else {
    Write-Host "[3/4] config.json 已存在，跳过" -ForegroundColor Green
}

# 4. sanity checks
Write-Host "[4/4] 环境自检..." -ForegroundColor Green
$napcatUp = (netstat -ano | Select-String ":3001" | Select-String "LISTENING")
$cdpUp = (netstat -ano | Select-String ":9229" | Select-String "LISTENING")
if ($napcatUp) { Write-Host "      NapCat WS (3001): 已监听" -ForegroundColor Green }
else { Write-Host "      NapCat WS (3001): 未监听 - 请先启动 NapCat 并登录小号" -ForegroundColor Yellow }
if ($cdpUp) { Write-Host "      Codex CDP (9229): 已监听" -ForegroundColor Green }
else { Write-Host "      Codex CDP (9229): 未监听 - 请用 Codex++ 启动桌面 Codex" -ForegroundColor Yellow }

Write-Host ""
Write-Host "安装完成。启动方式：" -ForegroundColor Cyan
Write-Host "  前台:  python bridge.py" -ForegroundColor White
Write-Host "  无窗口: pythonw bridge.py" -ForegroundColor White
