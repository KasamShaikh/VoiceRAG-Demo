# Phase 3 - run the bridge locally with environment loaded from azd.
#
# Prereqs:
#   - `azd up` has run (so the env has AZURE_OPENAI_*, AZURE_SEARCH_*, etc.)
#   - You have data-plane RBAC on AOAI + AI Search (azd env set AZURE_PRINCIPAL_ID).
#   - REDIS_PASSWORD optional; if empty, in-memory cache is used.

$ErrorActionPreference = 'Stop'

Push-Location (Resolve-Path "$PSScriptRoot/..")
try {
    Write-Host "==> Loading azd env values ..." -ForegroundColor Cyan
    $envValues = & azd env get-values
    foreach ($line in $envValues) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*"?([^"]*)"?\s*$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
        }
    }

    if (-not $env:REDIS_PASSWORD -and $env:REDIS_HOST) {
        Write-Host "REDIS_PASSWORD not set; bridge will fall back to in-memory cache." -ForegroundColor Yellow
        Write-Host "To enable Redis: az redis list-keys -g <rg> -n <name> --query primaryKey -o tsv | % { azd env set REDIS_PASSWORD $_ }" -ForegroundColor Yellow
    }

    Write-Host "==> Installing bridge deps ..." -ForegroundColor Cyan
    python -m pip install -q -r "bridge/requirements.txt"

    Write-Host "==> Starting bridge on http://127.0.0.1:8000 ..." -ForegroundColor Cyan
    python -m uvicorn bridge.main:app --host 127.0.0.1 --port 8000 --reload
}
finally {
    Pop-Location
}
