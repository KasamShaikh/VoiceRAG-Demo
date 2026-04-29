# Phase 2 - upload PDFs to blob and (re)build the AI Search index.
#
# Run this AFTER `azd up` has provisioned infra in Sweden Central.
# Requires:  Python 3.11+, `az login` (or `azd auth login`).

$ErrorActionPreference = 'Stop'

Push-Location $PSScriptRoot
try {
    Write-Host "==> Loading azd env values into the current process ..." -ForegroundColor Cyan
    $envValues = & azd env get-values 2>$null
    if (-not $envValues) {
        throw "azd env not initialized. Run 'azd env new voicebot-swc' and 'azd up' first."
    }
    foreach ($line in $envValues) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*"?([^"]*)"?\s*$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
        }
    }

    # Subscription + RG aren't always emitted by azd; derive from azd defaults.
    if (-not $env:AZURE_SUBSCRIPTION_ID) {
        $env:AZURE_SUBSCRIPTION_ID = (& az account show --query id -o tsv)
    }
    if (-not $env:AZURE_RESOURCE_GROUP) {
        $env:AZURE_RESOURCE_GROUP = (& azd env get-value AZURE_RESOURCE_GROUP 2>$null)
        if (-not $env:AZURE_RESOURCE_GROUP) {
            $env:AZURE_RESOURCE_GROUP = "rg-$env:AZURE_ENV_NAME"
        }
    }

    Write-Host "Subscription: $env:AZURE_SUBSCRIPTION_ID"
    Write-Host "RG:           $env:AZURE_RESOURCE_GROUP"
    Write-Host "Storage:      $env:AZURE_STORAGE_ACCOUNT"
    Write-Host "Search:       $env:AZURE_SEARCH_ENDPOINT"
    Write-Host "AOAI:         $env:AZURE_OPENAI_ENDPOINT`n"

    Write-Host "==> Installing Python deps ..." -ForegroundColor Cyan
    python -m pip install -q -r "indexer/requirements.txt"

    Write-Host "==> Uploading PDFs from ./data ..." -ForegroundColor Cyan
    python "indexer/upload_pdfs.py"

    Write-Host "==> Creating index + skillset + indexer, running it ..." -ForegroundColor Cyan
    python "indexer/run_indexer.py"
}
finally {
    Pop-Location
}
