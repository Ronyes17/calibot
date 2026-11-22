# Nikol deploy: packs backend\app (code only), uploads, restarts, checks.
# Usage (PowerShell, in the calibot folder):  .\deploy.ps1
$ErrorActionPreference = "Stop"
$Server = "root@82.197.66.185"
$Pkg = Join-Path $env:TEMP "nikol-app.tar.gz"

Set-Location $PSScriptRoot
Write-Host "1/3 packing code..."
tar -czf $Pkg --exclude=__pycache__ --exclude=*.db --exclude=*.pickle --exclude=*.json --exclude=.env -C backend app

Write-Host "2/3 uploading..."
scp $Pkg "${Server}:/tmp/nikol-app.tar.gz"

Write-Host "3/3 installing and restarting..."
ssh $Server "set -e; cd /opt/assistant/backend; tar -czf /opt/assistant/app-prev.tar.gz app; tar -xzf /tmp/nikol-app.tar.gz -C /opt/assistant/backend; rm -f /tmp/nikol-app.tar.gz; if id nikol >/dev/null 2>&1; then chown -R nikol:nikol /opt/assistant; fi; systemctl restart assistant; sleep 4; systemctl is-active assistant || (journalctl -u assistant -n 25 --no-pager; exit 1)"

Write-Host "Done. Nikol is running the new code." -ForegroundColor Green
Write-Host "Rollback if needed:  ssh $Server 'cd /opt/assistant/backend && tar -xzf ../app-prev.tar.gz && systemctl restart assistant'"
