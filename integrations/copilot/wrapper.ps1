# cortana-tts copilot wrapper
# Wraps `gh copilot` to speak responses via cortana-tts (Windows PowerShell).
# Append to your PowerShell profile or install via: cortana-tts install copilot

function Invoke-AraTtsCopilot {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Args)

    $CORTANA_TTS_URL = if ($env:CORTANA_TTS_SERVER) { $env:CORTANA_TTS_SERVER } else { "http://127.0.0.1:5111" }

    # Run gh copilot and capture output
    $output = & gh copilot @Args 2>&1 | Out-String
    Write-Output $output

    # Extract meaningful response lines (skip prompt/UI lines starting with ? or #)
    $lines = $output -split "`n" | Where-Object { $_ -notmatch "^\?" -and $_ -notmatch "^#" -and $_.Trim() -ne "" }
    $speakLines = $lines | Select-Object -Last 20 | Select-Object -First 5
    $speakText = ($speakLines -join " ").Trim()

    if ($speakText) {
        $body = @{ text = $speakText } | ConvertTo-Json -Compress
        Start-Job -ScriptBlock {
            param($Url, $Body)
            Invoke-RestMethod -Uri "$Url/speak" -Method Post -Body $Body -ContentType "application/json" -TimeoutSec 30
        } -ArgumentList $CORTANA_TTS_URL, $body | Out-Null
    }
}

function gh {
    if ($args[0] -eq "copilot") {
        Invoke-AraTtsCopilot @($args[1..($args.Count - 1)])
    } else {
        & (Get-Command gh -CommandType Application).Source @args
    }
}
