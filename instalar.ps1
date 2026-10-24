# Instala el puente Telegram <-> agentes (Claude Code / Codex) en este PC con Windows.
# Uso (desde la carpeta del repositorio):
#   powershell -ExecutionPolicy Bypass -File instalar.ps1 -BotName "@MiBot"
# Requisitos: Python 3.12, Git, Claude Code (`claude` con sesión iniciada) y/o Codex (`codex login`).
# Antes: copia .env.example a .env y rellénalo (ver INSTALAR.md).
param([Parameter(Mandatory = $true)][string]$BotName, [switch]$SinArranque)
$ErrorActionPreference = 'Stop'
$h = $env:USERPROFILE
$src = $PSScriptRoot
$dst = Join-Path $h 'agentes-telegram'

if (-not (Test-Path (Join-Path $src '.env'))) { throw 'Falta .env: copia .env.example a .env y rellena el token y tu ID de Telegram.' }

# 1. Código en ~/agentes-telegram (si el repo ya está ahí, no copia nada)
if ((Resolve-Path $src).Path -ne $dst) {
    New-Item -ItemType Directory -Force $dst | Out-Null
    Get-ChildItem $src -Force | Where-Object { $_.Name -notin '.git', '.venv' } | Copy-Item -Destination $dst -Recurse -Force
}
Set-Location $dst
'data', 'inbox', 'orquesta' | ForEach-Object { New-Item -ItemType Directory -Force $_ | Out-Null }

# 2. Rutas y nombre del bot en el prompt del sistema
$sp = Join-Path $dst 'system_prompt.md'
(Get-Content $sp -Raw -Encoding UTF8).Replace('{{HOME}}', $h).Replace('{{BOT}}', $BotName) |
    Set-Content $sp -Encoding UTF8

# 3. Entorno de Python
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

# 4. Guardián: hook de confirmación por Telegram para Claude (guard_settings.json) y Codex (~/.codex/hooks.json)
$py = ($dst -replace '\\', '/') + '/.venv/Scripts/python.exe'
$guard = ($dst -replace '\\', '/') + '/guard.py'
@{ hooks = @{ PreToolUse = @(@{ matcher = 'Bash|PowerShell'; hooks = @(@{ type = 'command'; command = "`"$py`" `"$guard`""; timeout = 960 }) }) } } |
    ConvertTo-Json -Depth 8 | Set-Content (Join-Path $dst 'guard_settings.json') -Encoding UTF8
$codexDir = Join-Path $h '.codex'
New-Item -ItemType Directory -Force $codexDir | Out-Null
$codexHooks = Join-Path $codexDir 'hooks.json'
if (Test-Path $codexHooks) { Copy-Item $codexHooks "$codexHooks.bak-$(Get-Date -Format 'yyyyMMddHHmmss')" }
@{ hooks = @{ PreToolUse = @(@{ matcher = '^(Bash|apply_patch)$'; hooks = @(@{ type = 'command'; command = "$py $guard"; timeout = 960 }) }) } } |
    ConvertTo-Json -Depth 8 | Set-Content $codexHooks -Encoding UTF8

# 5. Cerebro (memoria compartida de los agentes): solo se crea si no existe
$cerebro = Join-Path $h 'cerebro'
if (-not (Test-Path $cerebro)) { Copy-Item (Join-Path $dst 'cerebro-plantilla') $cerebro -Recurse }
$ref = "`n# Contexto global`nLee y mantén el cerebro en $cerebro según su INDEX.md.`n@$($cerebro -replace '\\', '/')/INDEX.md`n@$($cerebro -replace '\\', '/')/perfil.md`n@$($cerebro -replace '\\', '/')/notas.md`n"
foreach ($f in (Join-Path $h '.claude\CLAUDE.md'), (Join-Path $h '.codex\AGENTS.md')) {
    New-Item -ItemType Directory -Force (Split-Path $f) | Out-Null
    if (-not (Test-Path $f) -or -not (Select-String -Path $f -Pattern 'cerebro' -Quiet)) { Add-Content $f $ref -Encoding UTF8 }
}

# 6. Arranque automático con Windows (oculto; start_bridge.cmd lo relanza si se cae)
@"
@echo off
rem Arranca el puente y lo relanza si se cae.
cd /d $dst
:loop
.venv\Scripts\python.exe bot.py
timeout /t 10 /nobreak >nul
goto loop
"@ | Set-Content (Join-Path $dst 'start_bridge.cmd') -Encoding ASCII
if (-not $SinArranque) {
    $vbs = Join-Path ([Environment]::GetFolderPath('Startup')) 'agentes-telegram.vbs'
    Set-Content -Path $vbs -Encoding ASCII -Value "CreateObject(`"WScript.Shell`").Run `"`"`"$dst\start_bridge.cmd`"`"`", 0, False"
    Write-Host "Arranque automatico creado: $vbs"
}

# 7. Comprobación del guardián
& .\.venv\Scripts\python.exe test_guard.py
Write-Host "`nInstalado en $dst. Arranca ya con: wscript `"$([Environment]::GetFolderPath('Startup'))\agentes-telegram.vbs`""
Write-Host 'Luego escribe a tu bot en Telegram: /ayuda'
