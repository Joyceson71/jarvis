# Removes the Jarvis auto-start task.
param([string]$TaskName = "Jarvis")
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Auto-start removed."
