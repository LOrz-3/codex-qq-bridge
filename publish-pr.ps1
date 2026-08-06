<#
.SYNOPSIS
    Publish local changes as a GitHub PR (works when github.com:443 is not
    reachable - everything goes through api.github.com).
.DESCRIPTION
    Creates a feature branch off the current remote main, uploads the given
    local files via the Contents API, and opens a PR to main.
    Requires a GH_TOKEN env var (or -Token).
.PARAMETER Files
    Relative paths (space separated) of the files to publish.
.PARAMETER Branch
    Branch name for the PR (default: feat/auto-<timestamp>).
.PARAMETER Title
    PR title (default: "docs: update").
.PARAMETER Body
    PR body (default: brief note).
.PARAMETER Base
    Base branch (default: main).
.PARAMETER Repo
    "owner/repo" (default: LOrz-3/codex-qq-bridge).
.PARAMETER Token
    GitHub token; falls back to $env:GH_TOKEN.
.EXAMPLE
    .\publish-pr.ps1 -Files "README.md,core/engine.py" -Title "feat: xxx" -Body "yyy"
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Files,
    [string]$Branch = "",
    [string]$Title = "docs: update",
    [string]$Body = "Automated update via publish-pr.ps1",
    [string]$Base = "main",
    [string]$Repo = "LOrz-3/codex-qq-bridge",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not $Token) { $Token = $env:GH_TOKEN }
if (-not $Token) { Write-Host "[FAIL] 需要 GH_TOKEN 环境变量或 -Token 参数" -ForegroundColor Red; exit 1 }

$H = @{ "User-Agent" = "codex-qq-bridge"; "Authorization" = "Bearer $Token"; "X-GitHub-Api-Version" = "2022-11-28" }
$Api = "https://api.github.com/repos/$Repo"

function Invoke-Gh([string]$Method, [string]$Path, $BodyObj = $null) {
    $uri = $Api + $Path
    $params = @{ Method = $Method; Headers = $H; Uri = $uri; ContentType = "application/json; charset=utf-8" }
    if ($null -ne $BodyObj) { $params.Body = ([System.Text.Encoding]::UTF8.GetBytes(($BodyObj | ConvertTo-Json -Depth 20))) }
    return Invoke-RestMethod @params -TimeoutSec 60
}

# 1. latest main sha
$ref = Invoke-Gh "GET" "/git/ref/heads/$Base"
$baseSha = $ref.object.sha
Write-Host "[1/4] base=$Base sha=$($baseSha.Substring(0,7))" -ForegroundColor Green

# 2. create branch
if (-not $Branch) { $Branch = "feat/auto-" + (Get-Date -Format "yyyyMMdd-HHmmss") }
try {
    $null = Invoke-Gh "POST" "/git/refs" @{ ref = "refs/heads/$Branch"; sha = $baseSha }
    Write-Host "[2/4] branch=$Branch created" -ForegroundColor Green
} catch {
    Write-Host "[2/4] branch=$Branch already exists (will update)" -ForegroundColor Yellow
}

# 3. upload files
foreach ($f in $Files) {
    $f = $f.Trim()
    if (-not $f) { continue }
    $local = Join-Path $Root ($f -replace "/", "\")
    if (-not (Test-Path -LiteralPath $local)) { Write-Host "[skip] missing: $f" -ForegroundColor Yellow; continue }
    $content = [Convert]::ToBase64String([IO.File]::ReadAllBytes($local))
    $body = @{ message = $Title; content = $content; branch = $Branch }
    try {
        $cur = Invoke-Gh "GET" "/contents/$f?ref=$Branch"
        $body.sha = $cur.sha
    } catch { }
    $null = Invoke-Gh "PUT" "/contents/$f" $body
    Write-Host "      uploaded: $f" -ForegroundColor Green
}

# 4. open PR
$pr = Invoke-Gh "POST" "/pulls" @{ title = $Title; head = $Branch; base = $Base; body = $Body }
Write-Host "[4/4] PR: $($pr.html_url)" -ForegroundColor Cyan
Write-Host "      number=$($pr.number) state=$($pr.state)" -ForegroundColor Green
