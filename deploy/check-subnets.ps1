# deploy/check-subnets.ps1 - pre-deployment subnet plan validation
#
# Validates carrier.hosts.*.subnets + sandbox.backends.*.subnets from
# <RSI_HOME>/settings.yaml (default ./var relative to platform/) for
# global uniqueness and CIDR overlap, reusing rsi.doctor semantics.
# Exit code: 0 = pass, 1 = overlap/format error found.
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File deploy/check-subnets.ps1
# Optional env: RSI_HOME to point at the data root.

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

$repoRoot = Split-Path -Parent $PSScriptRoot
$platform = Join-Path $repoRoot "platform"
Set-Location $platform

$py = @'
import sys
from rsi.doctor import Doctor
from rsi.settings import Settings

results = Doctor()._check_subnets(Settings().model_dump())
for r in results:
    mark = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL"}[r.status]
    print(f"[{mark}] {r.name}: {r.message}")
    if r.status != "pass" and r.fix_hint:
        print(f"        fix: {r.fix_hint}")
sys.exit(1 if any(r.status == "fail" for r in results) else 0)
'@

# PS 5.1 strips quotes when passing -c to native processes; a temp file
# is the reliable way to hand the snippet to python.
$tmpPy = Join-Path ([System.IO.Path]::GetTempPath()) "rsi-check-subnets-$PID.py"
Set-Content -Path $tmpPy -Value $py -Encoding UTF8

try {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        uv run --no-sync python $tmpPy
    } else {
        $venvPy = Join-Path $platform ".venv\Scripts\python.exe"
        if (Test-Path $venvPy) {
            & $venvPy $tmpPy
        } else {
            python $tmpPy
        }
    }
    exit $LASTEXITCODE
} finally {
    Remove-Item $tmpPy -ErrorAction SilentlyContinue
}
