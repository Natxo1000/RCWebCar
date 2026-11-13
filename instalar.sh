#!/usr/bin/env bash
# =============================================================================
#  Instalador del Robot RC para Raspberry Pi OS (Bookworm o Bullseye)
#
#  Código: https://github.com/Natxo1000/RCWebCar
#
#  En la Pi:
#     curl -fsSLO https://raw.githubusercontent.com/Natxo1000/RCWebCar/main/instalar.sh
#     sudo bash instalar.sh
#  Si no encuentra el programa a su lado (robot_rc_software.zip o la carpeta
#  robot_rc de una copia del repositorio), lo descarga de GitHub.
#
#  Te hace unas preguntas y después instala y configura todo:
#  librerías, PWM de los servos, cámara, MediaMTX, servicios, nombre en la red,
#  IP fija, acceso desde Internet con login y Tailscale (opcionales).
#  Se puede volver a ejecutar para actualizar: guarda copia de lo anterior.
# =============================================================================
set -u

# ---------------------------------------------------------------- utilidades
if [ -t 1 ]; then
    C_OK=$'\e[32m'; C_AV=$'\e[33m'; C_ER=$'\e[31m'; C_TI=$'\e[1;36m'; C_B=$'\e[1m'; C_N=$'\e[0m'
else
    C_OK=""; C_AV=""; C_ER=""; C_TI=""; C_B=""; C_N=""
fi
titulo() { echo; echo "${C_TI}=== $* ===${C_N}"; }
ok()     { echo "${C_OK}✔ $*${C_N}"; }
aviso()  { echo "${C_AV}! $*${C_N}"; }
error()  { echo "${C_ER}✘ $*${C_N}" >&2; }
morir()  { error "$*"; exit 1; }

preguntar() {   # preguntar VARIABLE "texto" "valor por defecto"
    local __v=$1 __t=$2 __d=${3-} __r
    if [ -n "$__d" ]; then read -r -p "  $__t [$__d]: " __r; else read -r -p "  $__t: " __r; fi
    printf -v "$__v" '%s' "${__r:-$__d}"
}
si_no() {       # si_no "texto" s|n   -> devuelve 0 si la respuesta es sí
    local __d=${2:-s} __r __o
    [ "$__d" = s ] && __o="S/n" || __o="s/N"
    while true; do
        read -r -p "  $1 [$__o]: " __r
        __r=${__r:-$__d}
        case "${__r,,}" in
            s|si|sí|y|yes) return 0 ;;
            n|no) return 1 ;;
        esac
    done
}
contrasena() {  # contrasena VARIABLE "texto"
    local __v=$1 __a __b
    while true; do
        read -r -s -p "  $2: " __a; echo
        read -r -s -p "  Repítela: " __b; echo
        if [ "$__a" != "$__b" ]; then aviso "No coinciden, otra vez."; continue; fi
        if [ ${#__a} -lt 8 ] || [ ${#__a} -gt 64 ] || [[ "$__a" =~ [[:space:]] ]]; then
            aviso "Entre 8 y 64 caracteres y sin espacios."; continue
        fi
        printf -v "$__v" '%s' "$__a"; return
    done
}
ip_valida() { [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
pedir_ip() {    # pedir_ip VARIABLE "texto" "defecto"
    local __x
    while true; do
        preguntar __x "$2" "${3-}"
        if ip_valida "$__x"; then printf -v "$1" '%s' "$__x"; return; fi
        aviso "Eso no parece una IP (ejemplo: 192.168.1.50)."
    done
}

# ---------------------------------------------------------------- comprobaciones
[ "$(id -u)" -eq 0 ] || morir "Ejecútalo con sudo:   sudo bash $0"
[ -t 0 ] || morir "Este instalador hace preguntas: descárgalo primero y ejecútalo con   sudo bash instalar.sh"
[ -f /etc/os-release ] && . /etc/os-release
command -v apt-get >/dev/null || morir "Este instalador es para Raspberry Pi OS (Debian)."

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USUARIO="${SUDO_USER:-}"
echo
echo "${C_B}Robot RC · instalador${C_N}   (${PRETTY_NAME:-sistema desconocido}, $(uname -m))"
if [ -z "$USUARIO" ] || [ "$USUARIO" = root ]; then
    preguntar USUARIO "Usuario de la Pi que usará el robot" "pi"
fi
id "$USUARIO" >/dev/null 2>&1 || morir "No existe el usuario '$USUARIO'."
HOME_U="$(getent passwd "$USUARIO" | cut -d: -f6)"
APP="$HOME_U/robot_rc"
MTX="$HOME_U/mediamtx"

# dónde está el software: el zip junto al script, la carpeta robot_rc de una copia del
# repositorio, el propio script dentro de esa carpeta... y si no, se descarga de GitHub
REPO_ZIP="https://github.com/Natxo1000/RCWebCar/archive/refs/heads/main.zip"
ZIP=""; SRC_DIR=""; DESCARGAR=0
for c in "$DIR/robot_rc_software.zip" "$DIR/robot_rc" "$DIR" "$PWD/robot_rc_software.zip" "$HOME_U/robot_rc_software.zip"; do
    if [[ $c == *.zip ]]; then
        if [ -f "$c" ]; then ZIP="$c"; break; fi
    elif [ -f "$c/server.py" ] && [ -f "$c/static/index.html" ]; then
        SRC_DIR="$c"; break
    fi
done
[ -n "$ZIP" ] || [ -n "$SRC_DIR" ] || DESCARGAR=1

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
[ -f "$CONFIG" ] || morir "No encuentro config.txt en /boot/firmware ni en /boot."

ARCH="$(dpkg --print-architecture)"; MAQ="$(uname -m)"
case "$ARCH/$MAQ" in
    arm64/*)        MTX_PAT='linux_arm64(v8)?\.tar\.gz' ;;
    armhf/armv6l)   MTX_PAT='linux_armv6\.tar\.gz' ;;
    armhf/*)        MTX_PAT='linux_armv7\.tar\.gz' ;;
    *) morir "Arquitectura no soportada: $ARCH/$MAQ (¿es una Raspberry Pi?)" ;;
esac

# red actual
IFACE="$(ip route 2>/dev/null | awk '/^default/ {print $5; exit}')"
IFACE="${IFACE:-wlan0}"
CIDR_ACT="$(ip -4 -o addr show "$IFACE" 2>/dev/null | awk '{print $4; exit}')"
IP_ACT="${CIDR_ACT%/*}"; PFX_ACT="${CIDR_ACT#*/}"
[ "$PFX_ACT" = "$CIDR_ACT" ] && PFX_ACT=24
GW_ACT="$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}')"
NM=0; systemctl is-active --quiet NetworkManager 2>/dev/null && NM=1

# ================================================================= PREGUNTAS
titulo "1/6 · Nombre en la red"
echo "  Con él podrás entrar en  http://NOMBRE.local:8080  sin saber la IP."
preguntar HOSTN "Nombre del robot" "$(hostname)"
HOSTN="$(echo "$HOSTN" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9-')"
[ -n "$HOSTN" ] || HOSTN="$(hostname)"

titulo "2/6 · IP fija"
echo "  Ahora la Pi tiene ${C_B}${IP_ACT:-sin IP}${C_N} en ${IFACE} (router ${GW_ACT:-?})."
echo "  Con IP fija el robot siempre está en la misma dirección (necesario si abres puertos en el router)."
echo "  Alternativa: reservar la IP en el router (DHCP estático) y contestar que no."
IP_FIJA=0
if si_no "¿Poner IP fija?" n; then
    IP_FIJA=1
    pedir_ip IPF "IP del robot (fuera del rango DHCP del router)" "$IP_ACT"
    preguntar PFX "Máscara en bits (24 = 255.255.255.0)" "$PFX_ACT"
    pedir_ip GW "Puerta de enlace (el router)" "$GW_ACT"
    preguntar DNS "DNS (separados por espacio)" "$GW 1.1.1.1"
fi

titulo "3/6 · Sensor de batería"
INA=0
si_no "¿Has montado el INA219 (medidor de batería, opcional)?" n && INA=1

titulo "4/6 · Acceso desde Internet (opcional)"
echo "  Para que alguien conduzca desde fuera, sin VPN, con usuario y contraseña."
echo "  Necesitas un dominio con HTTPS y un proxy inverso (Synology, nginx...) que lleve a esta Pi, puerto 8080."
INET=0; TURN=0; DOMINIO=""; PROXY=""; ADM_U=""; ADM_P=""; CF_KEY=""; CF_TOK=""
if si_no "¿Configurar el acceso desde Internet?" n; then
    INET=1
    preguntar DOMINIO "Dominio (ejemplo: coche.midominio.com)" ""
    while [ -z "$DOMINIO" ]; do preguntar DOMINIO "Dominio" ""; done
    DOMINIO="${DOMINIO#https://}"; DOMINIO="${DOMINIO#http://}"; DOMINIO="${DOMINIO%%/*}"
    echo "  IP local del proxy inverso (el Synology, el router con nginx...). Todo lo que llegue"
    echo "  de ella pedirá login. Si no lo sabes, déjalo vacío: también se detecta por las cabeceras."
    preguntar PROXY "IP del proxy inverso" ""
    if [ -n "$PROXY" ] && ! ip_valida "$PROXY"; then aviso "IP no válida, la dejo vacía."; PROXY=""; fi
    echo
    echo "  Tu usuario de administrador (el que usarás para entrar desde Internet):"
    preguntar ADM_U "Usuario" "$USUARIO"
    contrasena ADM_P "Contraseña (8–64 caracteres, sin espacios)"
    echo
    echo "  ¿Por dónde va el vídeo desde Internet?"
    echo "   1) Abriendo el puerto 8189 (TCP) del router hacia esta Pi   (lo más sencillo)"
    echo "   2) Por el TURN de Cloudflare, sin abrir puertos             (cuenta de Cloudflare, pide tarjeta)"
    preguntar OPC "Opción" "1"
    if [ "$OPC" = 2 ]; then
        TURN=1
        preguntar CF_KEY "Cloudflare: Turn Token ID" ""
        preguntar CF_TOK "Cloudflare: API Token" ""
        [ -n "$CF_KEY" ] && [ -n "$CF_TOK" ] || { aviso "Faltan datos de Cloudflare: uso la opción 1."; TURN=0; }
    fi
fi

titulo "5/6 · Tailscale (opcional)"
echo "  VPN gratuita para entrar al robot desde fuera con tus dispositivos, sin abrir nada."
TS=0
si_no "¿Instalar Tailscale?" n && TS=1

titulo "6/6 · Instalación anterior"
CONSERVAR=0; ACT_MTX=1
if [ -f "$APP/server.py" ]; then
    echo "  Ya hay una instalación en $APP (se guardará una copia)."
    si_no "¿Conservar tus ajustes de server.py (calibración, inversiones, trims...)?" s && CONSERVAR=1
else
    echo "  Instalación nueva."
fi
if [ -x "$MTX/mediamtx" ]; then
    si_no "MediaMTX ya está instalado. ¿Actualizarlo a la última versión?" n || ACT_MTX=0
fi

# ---------------------------------------------------------------- resumen
titulo "Resumen"
echo "  Usuario:            $USUARIO   (programa en $APP)"
if [ $DESCARGAR = 1 ]; then echo "  Programa:           se descarga de GitHub (Natxo1000/RCWebCar)"
else echo "  Programa:           ${ZIP:-$SRC_DIR}"; fi
echo "  Nombre en la red:   $HOSTN   →  http://$HOSTN.local:8080"
if [ $IP_FIJA = 1 ]; then echo "  IP fija:            $IPF/$PFX  (router $GW, DNS $DNS) en $IFACE"
else echo "  IP:                 automática (ahora ${IP_ACT:-?})"; fi
echo "  INA219:             $([ $INA = 1 ] && echo sí || echo no)"
if [ $INET = 1 ]; then
    echo "  Internet:           https://$DOMINIO  · admin '$ADM_U' · vídeo $([ $TURN = 1 ] && echo 'TURN Cloudflare' || echo 'puerto 8189 TCP')"
else echo "  Internet:           no (solo red local / VPN)"; fi
echo "  Tailscale:          $([ $TS = 1 ] && echo sí || echo no)"
echo
si_no "¿Empezamos?" s || { echo "  Cancelado. No se ha cambiado nada."; exit 0; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
REINICIAR=0

# ================================================================= INSTALACIÓN
titulo "Paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q || aviso "apt-get update ha dado avisos (sigo)."
PAQ="python3-aiohttp python3-gpiozero curl ca-certificates"
apt-cache show python3-lgpio >/dev/null 2>&1 && PAQ="$PAQ python3-lgpio"
apt-cache show rpicam-apps-lite >/dev/null 2>&1 && ! dpkg -s rpicam-apps >/dev/null 2>&1 && PAQ="$PAQ rpicam-apps-lite"
# shellcheck disable=SC2086
apt-get install -y -q $PAQ || morir "No se han podido instalar los paquetes: $PAQ"
ok "Instalado: $PAQ"
if [ $INA = 1 ]; then
    apt-get install -y -q python3-smbus2 i2c-tools >/dev/null 2>&1         || apt-get install -y -q python3-smbus i2c-tools >/dev/null 2>&1         || aviso "No se ha podido instalar smbus2 (necesario para el INA219)."
    python3 -c "import smbus2" 2>/dev/null && ok "smbus2 para el INA219" || aviso "Falta smbus2: pip3 install smbus2 --break-system-packages"
fi

for g in gpio video render i2c; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$USUARIO"
done
ok "Usuario $USUARIO en los grupos gpio y video"

titulo "config.txt (PWM de los servos, audio, cámara)"
[ -f "$CONFIG.antes_robot_rc" ] || cp "$CONFIG" "$CONFIG.antes_robot_rc"
ANADIR=()
if ! grep -q '^dtoverlay=pwm-2chan' "$CONFIG"; then
    ANADIR+=("# Robot RC: PWM por hardware para los servos (GPIO 12 y 13)")
    ANADIR+=("dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4")
fi
if grep -q '^dtparam=audio=on' "$CONFIG"; then
    sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG"; REINICIAR=1
elif ! grep -q '^dtparam=audio=off' "$CONFIG"; then
    ANADIR+=("dtparam=audio=off")
fi
if grep -q '^camera_auto_detect=0' "$CONFIG"; then
    sed -i 's/^camera_auto_detect=0/camera_auto_detect=1/' "$CONFIG"; REINICIAR=1
elif ! grep -q '^camera_auto_detect=' "$CONFIG"; then
    ANADIR+=("camera_auto_detect=1")
fi
if [ $INA = 1 ] && ! grep -q '^dtparam=i2c_arm=on' "$CONFIG"; then
    ANADIR+=("dtparam=i2c_arm=on")
fi
if [ ${#ANADIR[@]} -gt 0 ]; then
    { echo; echo "[all]"; printf '%s\n' "${ANADIR[@]}"; } >> "$CONFIG"
    REINICIAR=1
fi
ok "config.txt listo (copia del original en $CONFIG.antes_robot_rc)"

titulo "Programa del robot"
if [ $DESCARGAR = 1 ]; then
    echo "  Descargando de GitHub..."
    curl -fsSL "$REPO_ZIP" -o "$TMP/github.zip" || morir "No he podido descargar $REPO_ZIP (¿hay Internet?)"
    ZIP="$TMP/github.zip"
    ok "Descargado"
fi
if [ -n "$SRC_DIR" ]; then          # por si el script está dentro de la carpeta que se va a sustituir
    cp -a "$SRC_DIR" "$TMP/src"; SRC_DIR="$TMP/src"
fi
COPIA=""
if [ -d "$APP" ]; then
    COPIA="$APP.copia-$(date +%Y%m%d-%H%M%S)"
    mv "$APP" "$COPIA"
    ok "Instalación anterior guardada en $COPIA"
fi
if [ -n "$ZIP" ]; then
    python3 -m zipfile -e "$ZIP" "$TMP/zip" || morir "No se ha podido descomprimir $ZIP"
    ORIG="$TMP/zip/robot_rc"
    [ -f "$ORIG/server.py" ] || ORIG="$(dirname "$(find "$TMP/zip" -name server.py | head -1)")"
else
    ORIG="$SRC_DIR"
fi
[ -f "$ORIG/server.py" ] || morir "El zip no contiene server.py"
mkdir -p "$APP"
cp -a "$ORIG/." "$APP/"
if [ -n "$COPIA" ]; then
    [ -f "$COPIA/acceso.json" ] && cp -a "$COPIA/acceso.json" "$APP/" && ok "Usuarios y claves (acceso.json) conservados"
    if [ $CONSERVAR = 1 ] && [ -f "$COPIA/server.py" ]; then
        python3 - "$COPIA/server.py" "$APP/server.py" <<'PY'
import re, sys
viejo = open(sys.argv[1], encoding="utf-8").read()
ruta = sys.argv[2]
nuevo = open(ruta, encoding="utf-8").read()
CLAVES = ["INVERT_IZQ", "INVERT_DER", "INTERCAMBIAR_LADOS", "COMPENSA_IZQ", "COMPENSA_DER", "MAX_SPEED",
          "GIRO_SITIO", "GIRO_MARCHA", "GIRO_EXPO", "PAN_MIN, PAN_MAX", "TILT_MIN, TILT_MAX", "INVERT_PAN",
          "INVERT_TILT", "PAN_TRIM", "TILT_TRIM", "PITO_ARRANQUE", "PITO_CONECTAR", "BAT_SENSOR", "VEL_GUEST"]
n = 0
for k in CLAVES:
    pat = re.compile(r"^" + re.escape(k) + r"\s*=.*$", re.M)
    a, b = pat.search(viejo), pat.search(nuevo)
    if a and b and a.group(0) != b.group(0):
        nuevo = nuevo.replace(b.group(0), a.group(0), 1)
        n += 1
open(ruta, "w", encoding="utf-8").write(nuevo)
print(f"  {n} ajustes copiados de la instalación anterior")
PY
    fi
fi
if [ $INA = 1 ]; then
    sed -i 's/^BAT_SENSOR = False/BAT_SENSOR = True/' "$APP/server.py"
    ok "BAT_SENSOR = True (INA219)"
fi
chown -R "$USUARIO:" "$APP"
ok "Programa instalado en $APP"

titulo "MediaMTX (vídeo de la cámara)"
mkdir -p "$MTX"
if [ $ACT_MTX = 1 ] || [ ! -x "$MTX/mediamtx" ]; then
    LOCAL="$(ls "$DIR"/mediamtx_*linux_arm*.tar.gz 2>/dev/null | head -1 || true)"
    if [ -n "$LOCAL" ]; then
        cp "$LOCAL" "$TMP/mtx.tgz"; ok "Uso $LOCAL"
    else
        URL="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
               | grep -oE '"browser_download_url": *"[^"]+"' | cut -d'"' -f4 | grep -E "$MTX_PAT" | head -1)"
        [ -n "$URL" ] || morir "No he podido encontrar la descarga de MediaMTX (¿hay Internet?). Puedes dejar el .tar.gz de MediaMTX junto a este script."
        echo "  Descargando $URL"
        curl -fL --progress-bar "$URL" -o "$TMP/mtx.tgz" || morir "Fallo al descargar MediaMTX"
    fi
    mkdir -p "$TMP/mtx"
    tar -xzf "$TMP/mtx.tgz" -C "$TMP/mtx" || morir "El archivo de MediaMTX está dañado"
    systemctl stop mediamtx 2>/dev/null || true
    install -m 755 "$TMP/mtx/mediamtx" "$MTX/mediamtx"
    cp "$TMP/mtx/mediamtx.yml" "$MTX/mediamtx.yml.original"
    ok "MediaMTX $("$MTX/mediamtx" --version 2>/dev/null | head -1) instalado en $MTX"
fi
BASE_YML="$MTX/mediamtx.yml.original"
if [ ! -f "$BASE_YML" ]; then            # MediaMTX puesto a mano antes: partir de su configuración
    cp "$MTX/mediamtx.yml" "$MTX/mediamtx.yml.original"
fi
[ -f "$MTX/mediamtx.yml" ] && cp "$MTX/mediamtx.yml" "$MTX/mediamtx.yml.copia-$(date +%Y%m%d-%H%M%S)"
MTX_BASE="$BASE_YML" MTX_OUT="$MTX/mediamtx.yml" DOMINIO="$DOMINIO" TURN="$TURN" python3 - <<'PY'
import os, re
txt = open(os.environ["MTX_BASE"], encoding="utf-8").read()
# quitar la sección paths: (va al final del archivo) y poner la nuestra después
m = re.search(r"^paths:.*\Z", txt, re.M | re.S)
if m:
    txt = txt[:m.start()]


def poner(clave, valor):
    global txt
    pat = re.compile(r"^" + re.escape(clave) + r":.*$", re.M)
    if pat.search(txt):
        txt = pat.sub(lambda _m: f"{clave}: {valor}", txt, count=1)
    else:
        txt = txt.rstrip("\n") + f"\n{clave}: {valor}\n"


for k in ("rtsp", "rtmp", "hls", "srt"):
    poner(k, "no")                                   # solo hace falta WebRTC
poner("webrtc", "yes")
poner("webrtcAddress", "127.0.0.1:8889")             # solo lo usa server.py (que pide login)
poner("webrtcLocalUDPAddress", "''")                 # vídeo solo por TCP: más estable al cambiar de red
poner("webrtcLocalTCPAddress", "':8189'")
poner("webrtcIPsFromInterfaces", "yes")
dom = os.environ.get("DOMINIO", "").strip()
poner("webrtcAdditionalHosts", f"[{dom}]" if dom else "[]")
if os.environ.get("TURN") == "1":
    poner("api", "yes")
    poner("apiAddress", "127.0.0.1:9997")
else:
    poner("api", "no")
txt = txt.rstrip("\n") + """

paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 1296
    rpiCameraHeight: 972
    rpiCameraFPS: 30
    rpiCameraDenoise: cdn_fast
"""
open(os.environ["MTX_OUT"], "w", encoding="utf-8").write(txt)
PY
[ $? -eq 0 ] || morir "No se ha podido preparar mediamtx.yml"
chown -R "$USUARIO:" "$MTX"
ok "mediamtx.yml preparado (cámara 1296×972 a 30 fps, WebRTC por TCP 8189)"

titulo "Servicios (arrancan solos al encender)"
for s in mediamtx robot_rc; do
    F="/etc/systemd/system/$s.service"
    if [ -f "$F" ] && [ ! -f "$F.antes_robot_rc" ]; then cp "$F" "$F.antes_robot_rc"; fi
done
cat > /etc/systemd/system/mediamtx.service <<EOF
[Unit]
Description=MediaMTX - camara del Robot RC
After=network-online.target
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$MTX
ExecStart=$MTX/mediamtx $MTX/mediamtx.yml
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
sed -e 's/\r$//' -e "s#/home/TU_USUARIO#$HOME_U#g" -e "s#TU_USUARIO#$USUARIO#g" "$APP/robot_rc.service" \
    > /etc/systemd/system/robot_rc.service
systemctl daemon-reload
systemctl enable mediamtx robot_rc >/dev/null 2>&1
ok "Servicios mediamtx y robot_rc activados"
OTRO="$(ss -ltnp 2>/dev/null | grep ':8080 ' | grep -v robot_rc || true)"
if [ -n "$OTRO" ] && ! systemctl is-active --quiet robot_rc; then
    aviso "Hay otro programa usando el puerto 8080:"; echo "    $OTRO"
    aviso "Páralo o cambia PORT en $APP/server.py."
fi

if [ $INET = 1 ]; then
    titulo "Acceso desde Internet"
    sudo -u "$USUARIO" env RC_DIR="$APP" RC_USER="$ADM_U" RC_PASS="$ADM_P" RC_DOM="$DOMINIO" \
        RC_PROXY="$PROXY" RC_KEY="$CF_KEY" RC_TOK="$CF_TOK" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["RC_DIR"])
import server as S
datos = S.leer_archivo_acceso()
datos["dominio"] = os.environ["RC_DOM"]
datos["proxy_ips"] = [ip for ip in os.environ.get("RC_PROXY", "").split() if ip]
if os.environ.get("RC_KEY"):
    datos["cloudflare_turn"] = {"key_id": os.environ["RC_KEY"], "api_token": os.environ["RC_TOK"]}
nombre = os.environ["RC_USER"]
claves = datos.setdefault("claves", {})
for c in [c for c, i in claves.items() if str(i.get("nombre", "")).casefold() == nombre.casefold()]:
    del claves[c]                       # al reinstalar, se sustituye su contraseña
clave, err = S.crear_clave(datos, nombre, "admin", os.environ["RC_PASS"])
if err:
    print("  ERROR:", err)
    sys.exit(1)
S.guardar_archivo_acceso(datos)
print(f"  Usuario admin '{nombre}' creado; dominio {datos['dominio']}")
PY
    [ $? -eq 0 ] && ok "acceso.json listo" || aviso "No se ha podido crear acceso.json (puedes hacerlo luego: python3 server.py clave ...)"
fi

titulo "Nombre e IP"
CAMBIA_NOMBRE=0
if [ "$HOSTN" != "$(hostname)" ]; then
    CAMBIA_NOMBRE=1
    if command -v raspi-config >/dev/null; then
        raspi-config nonint do_hostname "$HOSTN"
    else
        hostnamectl set-hostname "$HOSTN"
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTN/" /etc/hosts
    fi
    REINICIAR=1
    ok "Nombre cambiado a $HOSTN"
else
    ok "Nombre: $HOSTN"
fi
if [ $IP_FIJA = 1 ]; then
    if [ $NM = 1 ]; then
        CON="$(nmcli -g GENERAL.CONNECTION device show "$IFACE" 2>/dev/null | head -1)"
        if [ -n "$CON" ]; then
            nmcli con mod "$CON" ipv4.method manual ipv4.addresses "$IPF/$PFX" ipv4.gateway "$GW" ipv4.dns "$DNS" \
                && ok "IP fija $IPF en la conexión '$CON' (se aplica al reiniciar)" \
                || aviso "No se ha podido poner la IP fija con nmcli."
        else
            aviso "No encuentro la conexión de $IFACE en NetworkManager: pon la IP fija a mano o resérvala en el router."
        fi
    elif [ -f /etc/dhcpcd.conf ]; then
        sed -i '/# Robot RC: IP fija/,/# fin Robot RC/d' /etc/dhcpcd.conf
        cat >> /etc/dhcpcd.conf <<EOF
# Robot RC: IP fija
interface $IFACE
static ip_address=$IPF/$PFX
static routers=$GW
static domain_name_servers=$DNS
# fin Robot RC
EOF
        ok "IP fija $IPF en /etc/dhcpcd.conf (se aplica al reiniciar)"
    else
        aviso "No sé cómo poner la IP fija en este sistema: resérvala en el router."
    fi
    REINICIAR=1
fi

if [ $TS = 1 ]; then
    titulo "Tailscale"
    if ! command -v tailscale >/dev/null; then
        curl -fsSL https://tailscale.com/install.sh | sh || aviso "No se ha podido instalar Tailscale."
    fi
    if command -v tailscale >/dev/null; then
        echo "  Abre el enlace que sale a continuación e inicia sesión para añadir el robot a tu red:"
        tailscale up || aviso "Tailscale no se ha conectado; luego puedes ejecutar: sudo tailscale up"
    fi
fi

# ================================================================= FINAL
IP_FIN="${IPF:-$IP_ACT}"
titulo "¡Listo!"
echo "  Web del robot:      http://$HOSTN.local:8080    o    http://$IP_FIN:8080"
[ $INET = 1 ] && echo "  Desde Internet:     https://$DOMINIO   (usuario '$ADM_U')"
echo "  Ajustes:            $APP/server.py   (luego: sudo systemctl restart robot_rc)"
echo "  Registros:          journalctl -u robot_rc -n 50 --no-pager    ·    journalctl -u mediamtx -n 50 --no-pager"
echo "  Usuarios (Internet): cd $APP && python3 server.py claves   (o desde el botón 👥 de la web)"
if [ $INET = 1 ]; then
    echo
    echo "${C_B}  Te falta hacer fuera de la Pi:${C_N}"
    echo "   • Proxy inverso: https://$DOMINIO → http://$IP_FIN:8080, con WebSocket activado."
    if [ $TURN = 1 ]; then
        echo "   • Nada en el router: el vídeo va por el TURN de Cloudflare."
    else
        echo "   • Router: reenvía el puerto 8189 TCP a $IP_FIN (solo lleva vídeo de quien ya ha hecho login)."
    fi
fi
echo
if [ $REINICIAR = 1 ]; then
    aviso "Hace falta reiniciar para activar el PWM de los servos$([ $IP_FIJA = 1 ] && echo ', la IP fija')$([ $CAMBIA_NOMBRE = 1 ] && echo ' y el nombre')."
    if si_no "¿Reiniciar ahora?" s; then
        echo "  Reiniciando... Vuelve a entrar en un minuto en http://$HOSTN.local:8080"
        sleep 2; reboot
    else
        echo "  Reinicia cuando puedas:  sudo reboot"
    fi
else
    systemctl restart mediamtx robot_rc
    sleep 2
    systemctl is-active --quiet robot_rc && ok "El robot está en marcha" || aviso "robot_rc no arranca: mira journalctl -u robot_rc"
fi
