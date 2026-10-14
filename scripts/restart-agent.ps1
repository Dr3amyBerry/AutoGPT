param([switch]$StartOnly)
$ErrorActionPreference = 'Stop'
$agentRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $agentRoot '.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Agent Python environment is missing.' }
if (-not $env:RIGORCORE_AGENT_TOKEN) {
    $env:RIGORCORE_AGENT_TOKEN = [Environment]::GetEnvironmentVariable('RIGORCORE_AGENT_TOKEN', 'User')
}
if (-not $env:RIGORCORE_AGENT_TOKEN) {
    $env:RIGORCORE_AGENT_TOKEN = [Environment]::GetEnvironmentVariable('RIGORCORE_AGENT_TOKEN', 'Machine')
}
if (-not $env:RIGORCORE_AGENT_TOKEN) { throw 'Agent token is missing from the environment.' }
if (-not $StartOnly) {
    & (Join-Path $agentRoot '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'stop-local-agent.py')
    if ($LASTEXITCODE -ne 0) { throw 'Unable to stop the previous local agent.' }
}
$launched = Start-Process -FilePath $python -ArgumentList @('-m', 'rigorcore_dev.cli', 'tray') -WorkingDirectory $agentRoot -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 3
$launched.Refresh()
if ($launched.HasExited) { throw "Agent exited during startup (code $($launched.ExitCode))." }
Write-Output "Agent running: PID $($launched.Id)"
