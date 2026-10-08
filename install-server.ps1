$ErrorActionPreference = 'Stop'
$root = 'F:\website\backup-monitoring'
$python = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe'
$log = Join-Path $root 'install-result.json'
try {
    if (-not (Test-Path -LiteralPath $python)) { throw 'Python runtime not found' }
    & $python -c "import sys;sys.path.insert(0,r'$root');import server;server.setup();print('ready')" | Out-Null
    $action = New-ScheduledTaskAction -Execute $python -Argument 'server.py' -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName 'BackupMonitoringDashboard' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    $rule = Get-NetFirewallRule -DisplayName 'Backup Monitoring Dashboard (LAN)' -ErrorAction SilentlyContinue
    if (-not $rule) {
        New-NetFirewallRule -DisplayName 'Backup Monitoring Dashboard (LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8090 -RemoteAddress LocalSubnet -Profile Domain,Private | Out-Null
    }
    Start-ScheduledTask -TaskName 'BackupMonitoringDashboard'
    Start-Sleep -Seconds 4
    $task = Get-ScheduledTask -TaskName 'BackupMonitoringDashboard'
    $result = [ordered]@{ success = $true; task_state = $task.State.ToString(); timezone = (Get-TimeZone).Id; python = $python; port = 8090 }
} catch {
    $result = [ordered]@{ success = $false; error = $_.Exception.Message; trace = $_.ScriptStackTrace }
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $log -Encoding UTF8
