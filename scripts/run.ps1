$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
if (-not $env:API_KEY) { $env:API_KEY = 'local-demo-key' }
if (-not $env:API_KEY_ROLE) { $env:API_KEY_ROLE = 'admin' }
if (-not $env:API_KEY_ACTOR) { $env:API_KEY_ACTOR = 'local-demo-admin' }
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8103
