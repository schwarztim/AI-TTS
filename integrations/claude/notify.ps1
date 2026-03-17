# integrations/claude/notify.ps1 — ara-tts hook for Claude Code (Windows)
# Extracts <tts> tag from Claude's response and sends to TTS server.
# Handles: Stop, Notification, PermissionRequest, PreToolUse (immediate mode)
#
# Install via: ara-tts install claude

param()

$ErrorActionPreference = "SilentlyContinue"

$ARA_TTS_SERVER = if ($env:ARA_TTS_SERVER) { $env:ARA_TTS_SERVER } else { "http://127.0.0.1:5111" }
$LogDir = "$env:LOCALAPPDATA\ara-tts\logs"
$null = New-Item -ItemType Directory -Force -Path $LogDir
$LOGFILE = "$LogDir\ara-tts-hook.log"

$ConfigDir = "$env:APPDATA\ara-tts"
$TTS_TIMING_FILE = "$ConfigDir\tts_timing"

$RuntimeDir = "$env:TEMP\ara-tts-$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
$null = New-Item -ItemType Directory -Force -Path $RuntimeDir
$TTS_PLAYED_FLAG = "$RuntimeDir\tts_played"
$TTS_LAST_HASH = "$RuntimeDir\tts_last_hash"

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "HH:mm:ss.fff"
    Add-Content -Path $LOGFILE -Value "$ts [ara-tts] $Message"
}

function Get-Md5 {
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $hash = [System.Security.Cryptography.MD5]::Create().ComputeHash($bytes)
    return [System.BitConverter]::ToString($hash).Replace("-", "").ToLower()
}

function Extract-TtsFromText {
    param([string]$Text)
    $pattern = '(?:<!--\s*)?<tts(?:\s+mood="([^"]*)")?\s*>(.*?)</tts>(?:\s*-->)?'
    $m = [regex]::Match($Text, $pattern, [System.Text.RegularExpressions.RegexOptions]::Singleline)
    if ($m.Success) {
        return @{
            Text = $m.Groups[2].Value.Trim()
            Mood = $m.Groups[1].Value
        }
    }
    return @{ Text = ""; Mood = "" }
}

function Extract-TtsFromTranscript {
    param([string]$TranscriptPath)
    $result = @{ Text = ""; Mood = "" }
    if (-not (Test-Path $TranscriptPath)) { return $result }

    $entries = @()
    $lastUserPromptIdx = -1

    foreach ($line in Get-Content $TranscriptPath) {
        $line = $line.Trim()
        if (-not $line) { continue }
        try {
            $obj = $line | ConvertFrom-Json -ErrorAction Stop
            $entries += $obj
            $msg = $obj.message
            if ($msg -and $msg.role -eq "user") {
                $content = $msg.content
                if ($content -is [array]) {
                    $isToolResult = $content | Where-Object { $_ -is [psobject] -and $_.type -eq "tool_result" }
                    if (-not $isToolResult) {
                        $lastUserPromptIdx = $entries.Count - 1
                    }
                }
            }
        } catch {}
    }

    $searchFrom = $lastUserPromptIdx + 1
    for ($i = $searchFrom; $i -lt $entries.Count; $i++) {
        $obj = $entries[$i]
        $msg = $obj.message
        if (-not $msg -or $msg.role -ne "assistant") { continue }
        $content = $msg.content
        if (-not ($content -is [array])) { continue }
        foreach ($block in $content) {
            if ($block -is [psobject] -and $block.type -eq "text") {
                $extracted = Extract-TtsFromText -Text $block.text
                if ($extracted.Text) { return $extracted }
            }
        }
    }
    return $result
}

function Invoke-FireTts {
    param([string]$TtsText, [string]$Mood)
    $validMoods = @("error", "success", "warn", "melancholy")
    if ($Mood -and $validMoods -notcontains $Mood) { $Mood = "" }

    $body = @{ text = $TtsText }
    if ($Mood) { $body.mood = $Mood }
    $json = $body | ConvertTo-Json -Compress

    Write-Log "hook→server POST /speak"
    Start-Job -ScriptBlock {
        param($Url, $Json)
        Invoke-RestMethod -Uri "$Url/speak" -Method Post -Body $Json -ContentType "application/json" -TimeoutSec 10
    } -ArgumentList $ARA_TTS_SERVER, $json | Out-Null
}

# Read input from stdin
$INPUT = $input | Out-String
if (-not $INPUT) { $INPUT = [Console]::In.ReadToEnd() }

try { $data = $INPUT | ConvertFrom-Json } catch { exit 0 }
$HOOK_EVENT = if ($data.hook_event_name) { $data.hook_event_name } else { "unknown" }

Write-Log "hook entry event=$HOOK_EVENT"

$TTS_TIMING = "on-complete"
if (Test-Path $TTS_TIMING_FILE) {
    $TTS_TIMING = (Get-Content $TTS_TIMING_FILE -Raw).Trim()
}

# --- Notification: skip ---
if ($HOOK_EVENT -eq "Notification") {
    Write-Log "Notification event, skipping"
    exit 0
}

# --- PreToolUse: immediate mode ---
if ($HOOK_EVENT -eq "PreToolUse") {
    if ($TTS_TIMING -ne "immediate") { exit 0 }
    if (Test-Path $TTS_PLAYED_FLAG) { exit 0 }

    $transcriptPath = $data.transcript_path
    if (-not $transcriptPath -or -not (Test-Path $transcriptPath)) { exit 0 }

    $extracted = Extract-TtsFromTranscript -TranscriptPath $transcriptPath
    if ($extracted.Text) {
        $newHash = Get-Md5 -Text $extracted.Text
        $oldHash = if (Test-Path $TTS_LAST_HASH) { (Get-Content $TTS_LAST_HASH -Raw).Trim() } else { "" }
        if ($newHash -eq $oldHash) {
            Write-Log "immediate TTS: skipping stale duplicate"
            exit 0
        }
        Write-Log "immediate TTS: $($extracted.Text.Substring(0, [Math]::Min(80, $extracted.Text.Length)))..."
        $null = New-Item -Path $TTS_PLAYED_FLAG -ItemType File -Force
        Set-Content -Path $TTS_LAST_HASH -Value $newHash
        Invoke-FireTts -TtsText $extracted.Text -Mood $extracted.Mood
    }
    exit 0
}

# --- Stop in immediate mode ---
if ($HOOK_EVENT -eq "Stop" -and $TTS_TIMING -eq "immediate") {
    Remove-Item -Path $TTS_PLAYED_FLAG -Force -ErrorAction SilentlyContinue
    $response = if ($data.last_assistant_message) { $data.last_assistant_message.Substring(0, [Math]::Min(5000, $data.last_assistant_message.Length)) } else { "" }
    $extracted = Extract-TtsFromText -Text $response
    if ($extracted.Text) {
        Write-Log "immediate Stop: final TTS: $($extracted.Text.Substring(0, [Math]::Min(80, $extracted.Text.Length)))..."
        $newHash = Get-Md5 -Text $extracted.Text
        Set-Content -Path $TTS_LAST_HASH -Value $newHash
        Invoke-FireTts -TtsText $extracted.Text -Mood $extracted.Mood
    } else {
        Write-Log "immediate Stop: no TTS in final message, skipping"
    }
    exit 0
}

# --- Stop / on-complete mode ---
$rawMsg = if ($data.message) { $data.message } elseif ($data.title) { $data.title } elseif ($data.last_assistant_message) { $data.last_assistant_message } else { "" }
$response = if ($rawMsg) { $rawMsg.ToString().Substring(0, [Math]::Min(5000, $rawMsg.ToString().Length)) } else { "" }

$extracted = Extract-TtsFromText -Text $response

if (-not $extracted.Text -and $HOOK_EVENT -eq "Stop") {
    $transcriptPath = $data.transcript_path
    if ($transcriptPath -and (Test-Path $transcriptPath)) {
        $extracted = Extract-TtsFromTranscript -TranscriptPath $transcriptPath
        if ($extracted.Text) { Write-Log "TTS found via transcript fallback" }
    }
}

Write-Log "tts_text=$($extracted.Text.Substring(0, [Math]::Min(80, $extracted.Text.Length)))..."

if (-not $extracted.Text) {
    Write-Log "No <tts> tag found, playing random alert"
    Start-Job -ScriptBlock {
        param($Url)
        Invoke-RestMethod -Uri "$Url/alert" -Method Post -TimeoutSec 10
    } -ArgumentList $ARA_TTS_SERVER | Out-Null
    exit 0
}

$newHash = Get-Md5 -Text $extracted.Text
Set-Content -Path $TTS_LAST_HASH -Value $newHash
Invoke-FireTts -TtsText $extracted.Text -Mood $extracted.Mood
Remove-Item -Path $TTS_PLAYED_FLAG -Force -ErrorAction SilentlyContinue
exit 0
