# EP PTV trainings sync -- local Task Scheduler wrapper.
#
# Runs the trainings GUI scrape (sync_ptv_trainings.py) under the ep-syncs venv
# and writes a dated log under logs\ (gitignored). This sync is LOCAL-ONLY: PTV
# has no admin API, so it rides a browser session + an Outlook magic-code login
# and cannot run on Civis. Auth self-heals -- the sync verifies headless
# (silent when the session is live) and opens a headed Outlook relogin only if
# the session died -- so the machine must be logged on at run time.
#
# Registered as scheduled task "EP PTV Trainings Sync" (daily ~4 AM). See
# docs/ptv_trainings_sync_spec.md section 7. Do NOT add to
# civis/SCHEDULED_SCRIPTS.md -- that file is only for jobs that run on Civis.
#
# KEEP THIS FILE PURE ASCII. Windows PowerShell 5.1 reads a BOM-less UTF-8 file
# as ANSI, so a non-ASCII character inside a string breaks parsing.

$repo   = Split-Path $PSScriptRoot -Parent
$py     = 'C:\venvs\ep-syncs\Scripts\python.exe'
$script = Join-Path $repo 'sync_ptv_trainings.py'

$logDir = Join-Path $repo 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
$log    = Join-Path $logDir "ptv_trainings_$stamp.log"

# Force UTF-8 I/O so training names with non-ASCII characters don't crash the
# process on the default Windows cp1252 console encoding.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'

Set-Location $repo
"=== EP PTV trainings sync start $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8

& $py $script 2>&1 | ForEach-Object { $_.ToString() } | Out-File -FilePath $log -Append -Encoding utf8
$code = $LASTEXITCODE

"=== end $(Get-Date -Format o) exit=$code ===" | Out-File -FilePath $log -Append -Encoding utf8

# Prune logs older than 30 days.
Get-ChildItem -Path $logDir -Filter 'ptv_trainings_*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
