# Current-user background task. The legacy SYSTEM task is removed explicitly.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Enable', 'Disable', 'Status', 'Check')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Stop-ManagedTask {
    param([string]$Name, [string]$TaskFolder)
    Disable-ScheduledTask -TaskName $Name -TaskPath $TaskFolder | Out-Null
    Stop-ScheduledTask -TaskName $Name -TaskPath $TaskFolder
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $state = (Get-ScheduledTask -TaskName $Name -TaskPath $TaskFolder).State
        if ($state -notin @('Running', 'Queued')) { return }
        if ((Get-Date) -gt $deadline) { throw 'Task did not stop. Check Task Scheduler before updating files.' }
        Start-Sleep -Milliseconds 200
    } while ($true)
}

try {
    Import-Module ScheduledTasks -ErrorAction Stop
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $projectRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $runner = Join-Path $projectRoot 'background.py'
    $launcher = Join-Path $projectRoot 'run_background.ps1'
    $shell = Join-Path $PSHOME 'powershell.exe'

    # Interactive + Limited uses the logged-in account, including its VPN session.
    # python.exe is started hidden by the launcher; pythonw.exe is not used.
    $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcher + '"'
    $taskAction = New-ScheduledTaskAction -Execute $shell -Argument $arguments -WorkingDirectory $projectRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $trigger.Delay = 'PT30S'
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    if ($Action -eq 'Check') {
        # Validate scripts and task construction on Windows without registering anything.
        foreach ($script in @($PSCommandPath, $launcher)) {
            $tokens = $null
            $parseErrors = $null
            [System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$parseErrors) | Out-Null
            if (@($parseErrors).Count -ne 0) { throw "PowerShell syntax check failed: $script" }
        }
        if ([string]$taskPrincipal.LogonType -notin @('Interactive', '3')) { throw 'Expected interactive logon.' }
        if ([string]$taskPrincipal.RunLevel -notin @('Limited', '0')) { throw 'Expected limited privileges.' }
        if ($trigger.UserId -ne $identity.Name) { throw 'Logon trigger has the wrong user.' }
        Write-Host 'Autostart check OK: scripts parsed; current-user logon task constructed. No task was installed.'
        exit 0
    }

    $digest = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($projectRoot.ToUpperInvariant())
        $suffix = ([BitConverter]::ToString($digest.ComputeHash($bytes))).Replace('-', '').Substring(0, 12)
    } finally {
        $digest.Dispose()
    }
    $taskName = "FC-DKP-$suffix"
    $taskPath = '\'
    $legacyDescription = "FC DKP managed boot task v1 | $projectRoot"
    $description = "FC DKP managed user task v2 | $projectRoot"
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if ($existing -and $existing.Description -notin @($description, $legacyDescription)) {
        throw "Task name is occupied by an unrelated task: $taskName. Nothing was changed."
    }

    if ($Action -eq 'Disable') {
        if ($existing) {
            if ($existing.Description -eq $legacyDescription -and -not $isAdmin) {
                throw 'Legacy SYSTEM task found. Run autostart_off.bat as administrator ONCE to remove it.'
            }
            Stop-ManagedTask -Name $taskName -TaskFolder $taskPath
            Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
        }
        Write-Host 'Autostart removed. Settings and DKP data are kept.'
        exit 0
    }

    if ($Action -eq 'Enable') {
        if ($existing -and $existing.Description -eq $legacyDescription) {
            throw 'Remove the legacy task first: run autostart_off.bat as administrator, then autostart_on.bat normally.'
        }
        if ($isAdmin) {
            throw 'For VPN compatibility, close this window and double-click autostart_on.bat WITHOUT Run as administrator.'
        }
        foreach ($required in @($python, $runner, $launcher, (Join-Path $projectRoot '.env'))) {
            if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
                throw "Required file is missing: $required. Check the extracted files and run install.bat."
            }
        }
        if ($existing) { Stop-ManagedTask -Name $taskName -TaskFolder $taskPath }
        & $python -X utf8 $runner --check
        if ($LASTEXITCODE -ne 0) { throw 'Preflight failed. Stop start.bat before enabling autostart.' }
        Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $taskAction `
            -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Description $description -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
        Write-Host 'Autostart enabled: current user, hidden python.exe, now and at Windows SIGN-IN.'
        Write-Host 'Keep your VPN connected. Startup failures retry after 1 minute (up to 999 attempts).'
    }

    $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host 'Autostart is not installed for this folder. Use autostart_on.bat.'
        exit 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath $taskPath
    Write-Host "Task: $taskName"
    Write-Host "Account: $($task.Principal.UserId)"
    Write-Host "Logon type: $($task.Principal.LogonType)"
    Write-Host "State: $($task.State)"
    Write-Host "Last start: $($info.LastRunTime)"
    Write-Host ('Last result: 0x{0:X8}' -f [long]$info.LastTaskResult)
    Write-Host "Folder: $projectRoot"
    Write-Host 'Running means the process is active; verify /dkp status in Discord.'
    $log = Join-Path $projectRoot 'logs\background.log'
    if (Test-Path -LiteralPath $log) {
        # Old successful connections must not be mistaken for this task's readiness.
        $cutoff = $info.LastRunTime.AddSeconds(-1).ToString('yyyy-MM-dd HH:mm:ss,fff')
        $recent = @(Get-Content -LiteralPath $log -Encoding UTF8 -Tail 100 | Where-Object {
            $_.Length -ge 23 -and $_ -match '^\d{4}-\d{2}-\d{2} ' -and
            [string]::CompareOrdinal($_.Substring(0, 23), $cutoff) -ge 0
        })
        if ($recent.Count -gt 0) {
            Write-Host 'Background log since this task start:'
            $recent | Select-Object -Last 15 | ForEach-Object { Write-Host $_ }
        } else {
            Write-Host 'No entries for this start yet. Recheck autostart_status.bat shortly.'
        }
    } else {
        Write-Host 'No background log yet. Recheck autostart_status.bat shortly.'
    }
    exit 0
} catch {
    Write-Host ("Autostart operation failed: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
