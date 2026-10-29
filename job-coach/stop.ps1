. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$careerLock = Enter-CareerLock
function Stop-CareerProcess($Process) {
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) { $Process.Kill() }
        if (-not $Process.WaitForExit(5000)) { throw 'The owned process has not exited yet.' }
    } catch {
        $Process.Refresh()
        if (-not $Process.HasExited) { throw }
    }
}
try {
    $state = Read-CareerState
    foreach ($role in @('warmup','server')) {
        $process = Get-CareerOwnedProcess $state[$role]
        if ($process) { Stop-CareerProcess $process; Write-Host ('Stopped owned ' + $role + '.') }
        $state.Remove($role)
    }
    $ollama = Get-CareerOwnedProcess $state.ollama
    if ($ollama) {
        if ((Get-CareerListener 8512) -or (Get-CareerListener 8513)) {
            Write-Host 'The sharing service is still running; its model service was kept.'
        } else {
            # Unload before exiting our daemon so its runner releases model memory.
            if ((Get-CareerListener 11434) -eq $ollama.Id) {
                try {
                    $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:11434/api/generate')
                    $request.Proxy = $null; $request.Method = 'POST'; $request.ContentType = 'application/json'; $request.Timeout = 10000
                    $body = [Text.Encoding]::UTF8.GetBytes('{"model":"qwen3:1.7b","keep_alive":0}')
                    $request.ContentLength = $body.Length
                    $stream = $request.GetRequestStream()
                    try { $stream.Write($body,0,$body.Length) } finally { $stream.Dispose() }
                    $response = $request.GetResponse(); $response.Dispose()
                } catch { Write-Warning 'The model unload request did not finish; stopping the owned daemon.' }
            }
            Stop-CareerProcess $ollama
            $state.Remove('ollama')
            Write-Host 'Stopped the Ollama service started by this launcher.'
        }
    } else { $state.Remove('ollama') }
    Save-CareerState $state
    Write-Host 'Local launcher shutdown finished. Files and conversation records are unchanged.'
} catch {
    Write-Host ('Stop failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally { $careerLock.ReleaseMutex(); $careerLock.Dispose() }
