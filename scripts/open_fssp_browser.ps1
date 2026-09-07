param(
    [Parameter(Mandatory = $true)]
    [int]$BridgePid,

    [int]$DurationSeconds = 600
)

$ErrorActionPreference = "Stop"

$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$profile = Join-Path $PSScriptRoot "..\storage\chrome_fssp_profile"
$profile = [System.IO.Path]::GetFullPath($profile)

New-Item -ItemType Directory -Force -Path $profile | Out-Null

$chrome = Start-Process $chromePath -ArgumentList @(
    "--new-window",
    "--proxy-server=http://127.0.0.1:8899",
    "--proxy-bypass-list=<-loopback>",
    "--user-data-dir=$profile",
    "--no-first-run",
    "--disable-features=WinUseBrowserSpellChecker",
    "https://fssp.gov.ru/iss/ip/"
) -PassThru

$cleanupScript = "Start-Sleep -Seconds $DurationSeconds; Stop-Process -Id $($chrome.Id) -ErrorAction SilentlyContinue; Stop-Process -Id $BridgePid -ErrorAction SilentlyContinue"

Start-Process powershell -WindowStyle Hidden -ArgumentList @(
    "-NoProfile",
    "-Command",
    $cleanupScript
) | Out-Null

Write-Output "chrome_pid=$($chrome.Id) bridge_pid=$BridgePid duration_seconds=$DurationSeconds profile=$profile"
