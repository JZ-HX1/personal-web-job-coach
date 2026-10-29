$ErrorActionPreference = 'Stop'
$script:CareerRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$script:CareerRuntime = Join-Path $script:CareerRoot 'runtime'
$script:CareerState = Join-Path $script:CareerRuntime 'local-launcher.json'

function Get-CareerJson([string]$Url, [int]$Timeout = 700) {
    $request = [Net.HttpWebRequest]::Create($Url)
    $request.Proxy = $null
    $request.Timeout = $Timeout
    try {
        $response = $request.GetResponse()
        try {
            $reader = New-Object IO.StreamReader($response.GetResponseStream())
            try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
        } finally { $response.Dispose() }
    } catch { return $null }
}

function Get-CareerListener([int]$Port) {
    # netstat avoids first-use initialization of the NetTCPIP module.
    foreach ($line in (& "$env:SystemRoot/System32/netstat.exe" -ano -p tcp)) {
        $parts = $line.Trim() -split '\s+'
        if ($parts.Count -eq 5 -and $parts[0] -eq 'TCP' -and $parts[1] -match (':' + $Port + '$') -and $parts[3] -eq 'LISTENING') { return [int]$parts[4] }
    }
    return $null
}

function Test-CareerCommand($Record, [string]$CommandLine) {
    switch ($Record.role) {
        'server' { return ($CommandLine.Contains((Join-Path $script:CareerRoot 'server.py')) -and $CommandLine -match '--port\s+8501(?:\s|$)') }
        'warmup' { return $CommandLine.Contains((Join-Path $script:CareerRoot 'warm-model.ps1')) }
        'ollama' { return ([IO.Path]::GetFileName($Record.executable) -ieq 'ollama.exe' -and $CommandLine -match '(?:^|\s)serve\s*$') }
        default { return $false }
    }
}

function Get-CareerOwnedProcess($Record) {
    if (-not $Record -or $Record.pid -le 0 -or -not $Record.started -or -not $Record.executable) { return $null }
    try {
        $process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop
        if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne $Record.started -or $process.Path -ine $Record.executable) { return $null }
        $details = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$Record.pid) -ErrorAction Stop
        if (-not $details -or -not (Test-CareerCommand $Record $details.CommandLine)) { return $null }
        return $process
    } catch { return $null }
}

function New-CareerRecord($Process, [string]$Role) {
    return @{ pid=$Process.Id; started=$Process.StartTime.ToUniversalTime().Ticks.ToString(); executable=$Process.Path; role=$Role }
}

function Read-CareerState {
    if (-not (Test-Path -LiteralPath $script:CareerState)) { return @{} }
    $value = Get-Content -LiteralPath $script:CareerState -Raw | ConvertFrom-Json
    if ($value.root -ine $script:CareerRoot) { throw 'The launcher state belongs to a different project.' }
    $result = @{}
    foreach ($name in @('server', 'ollama', 'warmup')) { if ($value.$name) { $result[$name] = $value.$name } }
    return $result
}

function Save-CareerState($State) {
    $value = @{ root=$script:CareerRoot; version=1 }
    foreach ($name in @('server', 'ollama', 'warmup')) { if ($State[$name]) { $value[$name] = $State[$name] } }
    $temporary = $script:CareerState + '.tmp'
    $value | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $script:CareerState -Force
}

function Enter-CareerLock {
    New-Item -ItemType Directory -Path $script:CareerRuntime -Force | Out-Null
    $digest = [Security.Cryptography.SHA256]::Create()
    try { $name = [BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:CareerRoot.ToLowerInvariant()))).Replace('-', '') } finally { $digest.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\JobCoachLauncher_' + $name.Substring(0,24)))
    try { $locked = $mutex.WaitOne(20000) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { $mutex.Dispose(); throw 'Another start or stop is still running. Try again shortly.' }
    return $mutex
}

function Get-CareerModelConfig {
    $config = @{ MODEL_BASE_URL='http://127.0.0.1:11434/v1'; MODEL_NAME='qwen3:1.7b' }
    $dotenv = Join-Path $script:CareerRoot '.env'
    if (Test-Path -LiteralPath $dotenv) {
        foreach ($line in (Get-Content -LiteralPath $dotenv)) {
            if ($line -match '^\s*(MODEL_BASE_URL|MODEL_NAME)\s*=\s*(.*?)\s*$') { $config[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
        }
    }
    foreach ($key in @('MODEL_BASE_URL','MODEL_NAME')) {
        $configured = [Environment]::GetEnvironmentVariable($key)
        if ($configured) { $config[$key] = $configured }
    }
    return $config
}
