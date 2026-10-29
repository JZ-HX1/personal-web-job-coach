param([switch]$NoBrowser, [switch]$SkipWarmup)
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$careerLock = Enter-CareerLock
try {
    Set-Location -LiteralPath $script:CareerRoot
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    $state = Read-CareerState
    $listener = Get-CareerListener 8501
    $existing = Get-CareerOwnedProcess $state.server
    if ($listener) {
        $health = Get-CareerJson 'http://127.0.0.1:8501/health'
        if (-not $existing -or $existing.Id -ne $listener -or $health.app -ne 'job-coach' -or $health.status -ne 'ok') {
            throw 'Port 8501 is in use by an unverified process. It was not stopped or reused.'
        }
        Write-Host 'Job Coach is already running. Reusing the same local service.'
    } else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { throw 'Python 3.11+ is required. Add Python to PATH.' }
        $env:PYTHONUTF8 = '1'
        $env:CAREER_RUNTIME = Join-Path $script:CareerRuntime 'career'
        $server = Start-Process -FilePath $python.Source -ArgumentList @('-B','-X','utf8',('"' + (Join-Path $script:CareerRoot 'server.py') + '"'),'--port','8501') -WorkingDirectory $script:CareerRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $script:CareerRuntime 'local-server.log') -RedirectStandardError (Join-Path $script:CareerRuntime 'local-server.err.log') -PassThru
        $state.server = New-CareerRecord $server 'server'
        Save-CareerState $state
        $ready = $false
        for ($attempt=0; $attempt -lt 60; $attempt++) {
            $server.Refresh()
            if ($server.HasExited) { throw 'The server exited. See runtime/local-server.err.log.' }
            $health = Get-CareerJson 'http://127.0.0.1:8501/health' 300
            if ($health.app -eq 'job-coach' -and $health.status -eq 'ok') { $ready = $true; break }
            Start-Sleep -Milliseconds 100
        }
        if (-not $ready -or (Get-CareerListener 8501) -ne $server.Id) { throw 'The local server did not become ready. See runtime/local-server.err.log.' }
    }
    # Open the UI before model startup; no PDF installation or tunnel on this path.
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8501' }
    Write-Host 'Ready: http://127.0.0.1:8501 . Use stop.bat to close the local service.'
    $config = Get-CareerModelConfig
    if ($config.MODEL_BASE_URL -match '^http://(?:127\.0\.0\.1|localhost):11434/v1/?$') {
        $tags = Get-CareerJson 'http://127.0.0.1:11434/api/tags' 300
        if (-not $tags -and -not (Get-CareerListener 11434)) {
            $ollamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
            $ollamaPath = if ($ollamaCommand) { $ollamaCommand.Source } else { Join-Path $env:LOCALAPPDATA 'Programs/Ollama/ollama.exe' }
            if (Test-Path -LiteralPath $ollamaPath) {
                $env:OLLAMA_HOST = '127.0.0.1:11434'
                $env:OLLAMA_NO_CLOUD = '1'
                $env:OLLAMA_CONTEXT_LENGTH = '8192'
                $ollama = Start-Process -FilePath $ollamaPath -ArgumentList 'serve' -WorkingDirectory $script:CareerRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $script:CareerRuntime 'local-ollama.log') -RedirectStandardError (Join-Path $script:CareerRuntime 'local-ollama.err.log') -PassThru
                $state.ollama = New-CareerRecord $ollama 'ollama'
                Save-CareerState $state
            } else { Write-Warning 'Ollama is not installed. The workspace still works without AI chat.' }
        }
        if (-not $SkipWarmup -and $config.MODEL_NAME -eq 'qwen3:1.7b' -and -not (Get-CareerOwnedProcess $state.warmup)) {
            $warmup = Start-Process -FilePath "$env:SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe" -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + (Join-Path $script:CareerRoot 'warm-model.ps1') + '"')) -WorkingDirectory $script:CareerRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $script:CareerRuntime 'local-warmup.log') -RedirectStandardError (Join-Path $script:CareerRuntime 'local-warmup.err.log') -PassThru
            $state.warmup = New-CareerRecord $warmup 'warmup'
            Save-CareerState $state
        }
    }
} catch {
    Write-Host ('Start failed: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'If a background service was started, use stop.bat to close this launcher-owned service.'
    exit 1
} finally { $careerLock.ReleaseMutex(); $careerLock.Dispose() }
