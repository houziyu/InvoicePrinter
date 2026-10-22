$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
python make_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name "InvoicePrinter" `
  --icon "app.ico" `
  --add-data "LICENSE;." `
  --add-data "THIRD_PARTY_NOTICES.md;." `
  --add-data "licenses;licenses" `
  --collect-all pymupdf `
  --collect-all tkinterdnd2 `
  --hidden-import win32timezone `
  "invoice_printer.py"
if ($LASTEXITCODE -ne 0) { throw "EXE build failed. Close InvoicePrinter.exe and try again." }

Write-Host ""
Write-Host ""
Write-Host "Build complete: $ProjectDir\dist\InvoicePrinter.exe"
