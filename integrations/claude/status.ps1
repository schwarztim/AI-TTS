# integrations/claude/status.ps1 — Send overlay state updates on hook events (Windows)
# Used by: UserPromptSubmit, PreToolUse, SubagentStart, Stop
#
# Install via: cortana-tts install claude

param()

$ErrorActionPreference = "SilentlyContinue"

$CORTANA_TTS_SERVER = if ($env:CORTANA_TTS_SERVER) { $env:CORTANA_TTS_SERVER } else { "http://127.0.0.1:5111" }
$LogDir = "$env:LOCALAPPDATA\cortana-tts\logs"
$null = New-Item -ItemType Directory -Force -Path $LogDir
$LOGFILE = "$LogDir\cortana-tts-hook.log"

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "HH:mm:ss.fff"
    Add-Content -Path $LOGFILE -Value "$ts [cortana-tts] $Message"
}

function Post-Status {
    param([string]$Json)
    Start-Job -ScriptBlock {
        param($Url, $Json)
        Invoke-RestMethod -Uri "$Url/status" -Method Post -Body $Json -ContentType "application/json" -TimeoutSec 2
    } -ArgumentList $CORTANA_TTS_SERVER, $Json | Out-Null
}

function Test-ServerRunning {
    try {
        $r = Invoke-RestMethod -Uri "$CORTANA_TTS_SERVER/health" -TimeoutSec 1
        return $true
    } catch {
        return $false
    }
}

# Read input from stdin
$INPUT = $input | Out-String
if (-not $INPUT) { $INPUT = [Console]::In.ReadToEnd() }

try { $data = $INPUT | ConvertFrom-Json } catch { exit 0 }
$HOOK_EVENT = if ($data.hook_event_name) { $data.hook_event_name } else { "" }

Write-Log "status.ps1 event=$HOOK_EVENT"

switch ($HOOK_EVENT) {
    "UserPromptSubmit" {
        # Auto-start TTS server if not running
        if (-not (Test-ServerRunning)) {
            $araTts = Get-Command cortana-tts -ErrorAction SilentlyContinue
            if ($araTts) {
                Start-Process -FilePath $araTts.Source -ArgumentList "start --bg" -WindowStyle Hidden
                Write-Log "auto-starting server via cortana-tts start --bg"
                Start-Sleep -Seconds 2
            } else {
                Write-Log "cortana-tts not found in PATH, cannot auto-start"
            }
        }

        # Clear TTS played flag
        $RuntimeDir = "$env:TEMP\cortana-tts-$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
        Remove-Item -Path "$RuntimeDir\tts_played" -Force -ErrorAction SilentlyContinue

        # Inject TTS verbosity mode
        $ConfigDir = "$env:APPDATA\cortana-tts"
        $TTS_MODE_FILE = "$ConfigDir\tts_mode"
        if (Test-Path $TTS_MODE_FILE) {
            $mode = (Get-Content $TTS_MODE_FILE -Raw).Trim()
            Write-Output "tts_verbosity=$mode"
        } else {
            Write-Output "tts_verbosity=normal"
        }

        Post-Status -Json '{"state": "thinking"}'
    }

    "PreToolUse" {
        $toolName = if ($data.tool_name) { $data.tool_name } else { "" }
        $json = @{
            state     = "thinking"
            event     = "tool_use"
            tool_name = $toolName
        } | ConvertTo-Json -Compress
        Post-Status -Json $json
    }

    "SubagentStart" {
        Post-Status -Json '{"state": "thinking", "event": "subagent_start"}'
    }

    "Stop" {
        Post-Status -Json '{"state": "idle"}'
    }
}

exit 0
