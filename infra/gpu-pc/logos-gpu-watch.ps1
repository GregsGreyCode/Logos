# logos-gpu-watch.ps1: keeps the local model (llama-swap) off the GPU while a game runs.
#
# On the PC that runs the model. Every 10 seconds it looks for a running game:
#   - a game starts      -> llama-swap and llama-server are stopped (the GPU memory is freed at once);
#   - no game for 2 min  -> llama-swap is started again, hidden; the model loads on the next request.
# It also starts llama-swap at logon, so it doubles as the launcher.
# Logos handles the gap by itself: messages wait and are answered when the model is back
# (docs/model-downtime.md).
#
# Install (once, as yourself, no admin needed):
#   schtasks /Create /TN "Logos GPU watch" /SC ONLOGON /RL LIMITED /TR "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\llama\logos-gpu-watch.ps1"
#   schtasks /Run /TN "Logos GPU watch"
# Pause it (model stays off):  schtasks /End /TN "Logos GPU watch"  then stop llama-swap.
# Its log: C:\llama\gpu-watch.log

$Dir      = 'C:\llama'                       # llama-swap.exe, llama-server.exe and config.yaml
$Listen   = '0.0.0.0:8080'
$QuietFor = 120                              # seconds with no game before the model comes back

# A game is a process run from one of these folders...
$GameDirs = @('\steamapps\common\', '\Epic Games\', '\XboxGames\', '\GOG Galaxy\Games\', '\EA Games\')
# ...except these (launchers, crash reporters, helpers that live in the same folders).
$NotGames = @('EpicGamesLauncher', 'EpicWebHelper', 'UnrealCEFSubProcess', 'CrashReportClient',
              'steamwebhelper', 'GalaxyClient', 'EADesktop')
# Games installed anywhere else, by process name without .exe (e.g. 'javaw' for Minecraft).
$Extra    = @()

$Log = Join-Path $Dir 'gpu-watch.log'
function Say($text) { Add-Content $Log ("{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $text) }

function Find-Game {
    Get-Process | Where-Object {
        $path = $_.Path
        ($NotGames -notcontains $_.Name) -and (
            ($Extra -contains $_.Name) -or
            ($path -and ($GameDirs | Where-Object { $path -like "*$_*" })))
    } | Select-Object -First 1
}

function Start-Model {
    if (-not (Get-Process llama-swap -ErrorAction SilentlyContinue)) {
        Start-Process (Join-Path $Dir 'llama-swap.exe') -WorkingDirectory $Dir -WindowStyle Hidden `
            -ArgumentList '--config', 'config.yaml', '--listen', $Listen
        Say 'model on (llama-swap started)'
    }
}

function Stop-Model($why) {
    Stop-Process -Name llama-server, llama-swap -Force -ErrorAction SilentlyContinue
    Say "model off: $why"
}

$lastGame = [datetime]::MinValue
$off = $false
Say 'watch started'
while ($true) {
    $game = Find-Game
    if ($game) {
        $lastGame = Get-Date
        if (-not $off) { Stop-Model "$($game.Name) is running"; $off = $true }
    } elseif (((Get-Date) - $lastGame).TotalSeconds -ge $QuietFor) {
        Start-Model                               # after a game, at logon, or if it was closed
        $off = $false
    }
    Start-Sleep -Seconds 10
}
