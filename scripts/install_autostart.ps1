# Registers a scheduled task so Jarvis starts when you log in, unlock the PC,
# or wake the laptop from sleep. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "Jarvis"
)

$ErrorActionPreference = "Stop"
$user = "$env:USERDOMAIN\$env:USERNAME"

# Prefer the project's virtualenv; pythonw.exe runs without a console window.
$py = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { $py = (Get-Command pythonw.exe).Source }

$action = New-ScheduledTaskAction -Execute $py -Argument "run.py" -WorkingDirectory $ProjectDir

# 1) At log on
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user

# 2) On workstation unlock (StateChange 8 = SessionUnlock)
$unlockClass = Get-CimClass -ClassName MSFT_TaskSessionStateChangeTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$unlock = New-CimInstance -CimClass $unlockClass -ClientOnly -Property @{
    StateChange = 8
    UserId      = $user
    Enabled     = $true
}

# 3) On resume from sleep/hibernate (Power-Troubleshooter event 1)
$eventClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$resume = New-CimInstance -CimClass $eventClass -ClientOnly -Property @{
    Enabled      = $true
    Subscription = "<QueryList><Query Id='0' Path='System'><Select Path='System'>*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>"
}

# MultipleInstances IgnoreNew: if Jarvis is already running, extra triggers do nothing.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

# Interactive logon type: Jarvis needs your desktop session for mic, speakers and app control.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $unlock, $resume) `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Jarvis will now start at logon, unlock and wake from sleep."
Write-Host "Start it right now with:  Start-ScheduledTask -TaskName $TaskName"
