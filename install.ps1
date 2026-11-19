# LongArc one-line installer for Windows PowerShell
# Usage: irm https://raw.githubusercontent.com/.../install.ps1 | iex

$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $HOME ".longarc"
$BinDir = Join-Path $InstallDir "bin"

Write-Host "=== 安装 LongArc (Windows) ===" -ForegroundColor Cyan

# 1. 检查 Node.js 运行时
$nodeCmd = Get-Command "node" -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
    Write-Host "提示: 未检测到 Node.js，LongArc 交互界面需要 Node.js (>=18)。" -ForegroundColor Yellow
    Write-Host "请先下载安装 Node.js: https://nodejs.org" -ForegroundColor Gray
}

# 2. 创建安装目录并生成全局 cmd 启动脚本
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

$cmdPath = Join-Path $BinDir "longarc.cmd"
$cmdContent = @"
@echo off
setlocal
where longarc.cmd >nul 2>nul
if exist "%~dp0..\my-tui\bin\my-agent.js" (
    node "%~dp0..\my-tui\bin\my-agent.js" %*
) else (
    npx longarc %*
)
"@

[System.IO.File]::WriteAllText($cmdPath, $cmdContent, [System.Text.Encoding]::UTF8)

# 3. 环境变量 PATH 配置检查
$userPath = [Environment]::GetEnvironmentVariable("Path", [EnvironmentVariableTarget]::User)
if ($userPath -notlike "*$BinDir*") {
    $newPath = "$BinDir;$userPath"
    [Environment]::SetEnvironmentVariable("Path", $newPath, [EnvironmentVariableTarget]::User)
    $env:Path = "$BinDir;$env:Path"
    Write-Host "✓ 已将 $BinDir 永久加入当前用户的 PATH 环境变量" -ForegroundColor Green
}

Write-Host "✓ LongArc 安装就绪: $cmdPath" -ForegroundColor Green
Write-Host "提示: 新开 PowerShell 窗口后直接输入 'longarc' 即可体验！" -ForegroundColor Cyan
