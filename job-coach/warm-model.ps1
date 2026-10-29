. (Join-Path $PSScriptRoot 'launcher-common.ps1')
# Preload only a model already installed. This script never pulls a model.
for ($attempt=0; $attempt -lt 30; $attempt++) {
    $state = Read-CareerState
    if (-not (Get-CareerOwnedProcess $state.server)) { exit 0 }
    $tags = Get-CareerJson 'http://127.0.0.1:11434/api/tags' 400
    if ($tags) { break }
    Start-Sleep -Milliseconds 300
}
if (-not $tags -or @($tags.models | Where-Object { $_.name -eq 'qwen3:1.7b' }).Count -eq 0) {
    Write-Output 'No installed qwen3:1.7b was found. No download was attempted.'
    exit 0
}
$request = [Net.HttpWebRequest]::Create('http://127.0.0.1:11434/api/generate')
$request.Proxy = $null
$request.Method = 'POST'
$request.ContentType = 'application/json'
$request.Timeout = 180000
$body = [Text.Encoding]::UTF8.GetBytes('{"model":"qwen3:1.7b","prompt":"","stream":false,"keep_alive":"15m"}')
$request.ContentLength = $body.Length
try {
    $stream = $request.GetRequestStream()
    try { $stream.Write($body,0,$body.Length) } finally { $stream.Dispose() }
    $response = $request.GetResponse()
    $response.Dispose()
    Write-Output 'The existing Qwen3 1.7B model is ready in memory.'
} catch { Write-Output 'Model preload did not finish. Chat can still retry normally.' }
