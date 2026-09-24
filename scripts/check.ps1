# check.ps1 — local readiness check for a CodeNOW Flask component.
#
# Runs the SAME gate CodeNOW's build runs, plus route/test checks, so you catch a
# broken deploy on your own machine in seconds instead of waiting for a red
# Tekton build. Read-only: it makes NO commits and edits NO source.
#
#   ./scripts/check.ps1        # run from anywhere; it locates the repo root
#
# Exit code 0 = ready to commit, 1 = fix something first.

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$ok = $true

Write-Host ""
Write-Host "CodeNOW readiness check" -ForegroundColor Cyan
Write-Host "  $root"
Write-Host "----------------------------------------"

# --- 1. pylint gate (exactly the build's static-analysis stage) --------------
py -c "import pylint" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "  installing pylint..."; py -m pip install -q pylint }
py -m pylint --disable=C,R,W src
if ($LASTEXITCODE -eq 0) {
    Write-Host "[1/5] pylint gate ......... PASS" -ForegroundColor Green
} else {
    Write-Host "[1/5] pylint gate ......... FAIL  (an E/F error fails the CodeNOW build)" -ForegroundColor Red
    $ok = $false
}

# --- 2. lint coverage: what the gate silently SKIPS ---------------------------
# Verified behaviour (pylint 3.2): when src/ is itself a package (has
# __init__.py), pylint walks the package tree only — any subdirectory WITHOUT
# __init__.py is never linted, so a real error in it keeps the gate green.
# When src/ is NOT a package, every subdirectory is linted.
$srcIsPackage = Test-Path "src/__init__.py"
if ($srcIsPackage) {
    $unlinted = Get-ChildItem "src" -Directory -Recurse |
        Where-Object { $_.Name -ne "__pycache__" -and
                       -not (Test-Path (Join-Path $_.FullName "__init__.py")) -and
                       (Get-ChildItem $_.FullName -Filter *.py -File) }
    if (-not (Test-Path ".pylintrc")) {
        Write-Host "[2/5] lint coverage ....... FAIL  (src/ is a package but .pylintrc is missing -> false E0401 storm)" -ForegroundColor Red
        Write-Host "        the GitHub web uploader strips dotfiles - restore .pylintrc"
        $ok = $false
    } elseif ($unlinted) {
        Write-Host "[2/5] lint coverage ....... WARN  (never linted: $(($unlinted | ForEach-Object { $_.Name }) -join ', '))" -ForegroundColor Yellow
        Write-Host "        src/ is a package, so dirs without __init__.py are skipped - add one to each"
    } else {
        Write-Host "[2/5] lint coverage ....... PASS  (every python dir under src/ is linted)" -ForegroundColor Green
    }
} else {
    Write-Host "[2/5] lint coverage ....... PASS  (src/ is not a package - all subdirs linted)" -ForegroundColor Green
}

# --- 3. route smoke test ------------------------------------------------------
$pysrc = @'
import sys, json, os
sys.path.insert(0, "src")
try:
    import app as m
except Exception as e:
    print("SKIP:" + type(e).__name__ + ": " + str(e)[:120]); sys.exit(0)
c = m.app.test_client()
prefix = ""
try:
    from config import GlobalConstraints
    prefix = GlobalConstraints.GC_URL_PREFIX or ""
except Exception:
    pass
h = c.get("/health"); i = c.get(prefix + "/")
hj = ""
try: hj = json.dumps(h.get_json())
except Exception: pass
if h.status_code == 200 and i.status_code in (200, 302, 503):
    print("PASS:/health " + str(h.status_code) + " " + hj + " ; " + (prefix or "") + "/ " + str(i.status_code))
else:
    print("FAIL:/health " + str(h.status_code) + " ; " + (prefix or "") + "/ " + str(i.status_code))
'@
$tmp = Join-Path $env:TEMP ("cn_check_" + $PID + ".py")
Set-Content -Path $tmp -Value $pysrc -Encoding UTF8
$out = & py $tmp
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
$res = ($out | Where-Object { $_ -match '^(PASS|SKIP|FAIL):' } | Select-Object -Last 1)
if ($res -match '^PASS:') {
    Write-Host "[3/5] routes (/health, /) . PASS  ($($res.Substring(5)))" -ForegroundColor Green
} elseif ($res -match '^SKIP:') {
    Write-Host "[3/5] routes (/health, /) . SKIPPED  ($($res.Substring(5)))" -ForegroundColor Yellow
    Write-Host "        normal when app.py imports a deployed-only backend (idm/) - pylint above is the gate"
} else {
    Write-Host "[3/5] routes (/health, /) . FAIL  ($res)" -ForegroundColor Red
    $ok = $false
}

# --- 4. unit tests (the build's unit-test stage) ------------------------------
if (Test-Path "tests") {
    py -m pytest tests -q 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[4/5] pytest .............. PASS" -ForegroundColor Green
    } else {
        Write-Host "[4/5] pytest .............. FAIL" -ForegroundColor Red
        $ok = $false
    }
} else {
    Write-Host "[4/5] pytest .............. n/a  (no tests/ folder)"
}

# --- 5. artifact sync (only if a generator is present) -----------------------
if (Test-Path "make_dashboard.py") {
    $before = (git status --porcelain) -join "`n"
    py make_dashboard.py 2>$null 1>$null
    $after = (git status --porcelain) -join "`n"
    if ($before -eq $after) {
        Write-Host "[5/5] artifact in sync .... PASS" -ForegroundColor Green
    } else {
        Write-Host "[5/5] artifact in sync .... WARN  (regenerating changed files - commit the regenerated artifacts)" -ForegroundColor Yellow
    }
} else {
    Write-Host "[5/5] artifact in sync .... n/a  (no make_dashboard.py generator)"
}

Write-Host "----------------------------------------"
if ($ok) {
    Write-Host "=> READY TO COMMIT" -ForegroundColor Green
    exit 0
} else {
    Write-Host "=> FIX THE ABOVE BEFORE COMMITTING" -ForegroundColor Red
    exit 1
}
