[CmdletBinding()]
param(
    [ValidateRange(1, 1000)]
    [int]$Repeat = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path "evidence/junit" | Out-Null
New-Item -ItemType Directory -Force -Path "evidence/logs" | Out-Null

for ($run = 1; $run -le $Repeat; $run++) {
    Write-Host "=== test run $run/$Repeat ==="

    & python -m pytest -v `
        "--junitxml=evidence/junit/test-results.xml" `
        "--log-file=evidence/logs/pytest.log" `
        "--log-file-level=DEBUG"

    if ($LASTEXITCODE -ne 0) {
        throw "Test run $run failed with exit code $LASTEXITCODE"
    }
}
