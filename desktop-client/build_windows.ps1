$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.11 -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".venv\Scripts\python.exe" -m pytest

& ".venv\Scripts\pyinstaller.exe" `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name "CTExcelApplyClient" `
    --collect-all "playwright" `
    --collect-all "PySide6" `
    run.py

Write-Host ""
Write-Host "构建完成：$PSScriptRoot\dist\CTExcelApplyClient\CTExcelApplyClient.exe"
Write-Host "客户端默认调用 Windows 自带 Microsoft Edge。"
