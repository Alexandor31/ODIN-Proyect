# ============================================================================
# start_windows.ps1
# Arranca Jarvis Industrial y configura automáticamente el port-forward
# entre Windows y WSL para que http://localhost:8000 funcione en tu navegador.
#
# Uso (PowerShell, UNA vez si da error de ejecución):
#     Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#     .\start_windows.ps1
# ============================================================================

$ErrorActionPreference = "Stop"
$port = 8000

Write-Host ""
Write-Host "Jarvis Industrial · Arranque (Windows + WSL)" -ForegroundColor Cyan
Write-Host "=================================================" -ForegroundColor Cyan

# 1) Resolver la distro WSL predeterminada y su IP
Write-Host ""
Write-Host "[1/4] Detectando distro WSL..."
$distro = (wsl -l -q | Select-Object -First 1).Trim()
if (-not $distro) {
    Write-Host "ERROR: No se detecta ninguna distro WSL." -ForegroundColor Red
    exit 1
}
Write-Host "      Distro: $distro"

Write-Host "[2/4] Resolviendo IP de WSL..."
$wsl_ip = wsl -e bash -c "hostname -I 2>/dev/null | tr -s ' ' '\n' | grep -v '^127\.' | head -1"
if (-not $wsl_ip) {
    # fallback: usar la métrica del lado WSL
    $wsl_ip = wsl -e bash -c "cat /proc/net/route 2>/dev/null | awk 'NR>1 && /^[0-9a-f]/ {print \$1}' | head -1 | xargs -I{} sh -c 'ip -o -4 addr show dev {} 2>/dev/null | awk \"{print \\\$4}\" | cut -d/ -f1'"
    $wsl_ip = ($wsl_ip -split "`n")[0].Trim()
}
if (-not $wsl_ip) {
    Write-Host "AVISO: No se pudo obtener IP de WSL (probable modo NAT puro)." -ForegroundColor Yellow
}

if ($wsl_ip) {
    Write-Host "      WSL IP: $wsl_ip" -ForegroundColor Green

    # 2) Borrar reglas anteriores del mismo puerto
    Write-Host "[3/4] Configurando port-proxy Windows -> WSL:$port ..."
    netsh interface portproxy delete v4tov4 listenport=$port listenaddress=127.0.0.1 2>$null
    netsh interface portproxy add v4tov4 listenport=$port listenaddress=0.0.0.0 connectport=$port connectaddress=$wsl_ip | Out-Null

    # 3) Abrir el firewall si está cerrado
    $ruleName = "Jarvis Industrial WSL $port"
    netsh advfirewall firewall delete rule name=$ruleName 2>$null
    netsh advfirewall firewall add rule name=$ruleName dir=in action=allow protocol=TCP localport=$port | Out-Null
    Write-Host "      Regla de firewall creada." -ForegroundColor Green
} else {
    Write-Host "[3/4] Saltando port-proxy (no se detectó IP usable)." -ForegroundColor Yellow
    Write-Host "      Probable: WSL está en mirrored mode. localhost deberia funcionar directo." -ForegroundColor Yellow
}

# 4) Lanzar el server DENTRO de WSL (en segundo plano)
Write-Host ""
Write-Host "[4/4] Arrancando servidor en WSL ..."
Write-Host "      Cuando veas el BANNER ASCII, abre:" -ForegroundColor Cyan
Write-Host "          http://localhost:$port" -ForegroundColor Cyan -BackgroundColor Black
Write-Host "      Para parar: Ctrl+C en esta ventana."
Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host ""

wsl -e bash -c "cd /mnt/c/Users/HP/Documents/Seius/IAI/jarvis_industrial && source /mnt/c/Users/HP/Documents/Seius/IAI/.venv/bin/activate && python main.py"
