#!/usr/bin/env python3
"""Servidor del coche teledirigido.

- Sirve la web de control (static/index.html) en el puerto 8080.
- Recibe órdenes por WebSocket en /ws.
- Mueve los motores (TB6612FNG) y los servos pan/tilt.
- Mide la batería (INA219) y manda el porcentaje a la web.
- La cámara se mueve con la dirección del joystick; la posición la calcula la Pi.
- Watchdog: si no llegan órdenes de conducción en WATCHDOG_S segundos,
  para los motores.
- Opcional: acceso desde Internet con clave y vídeo por TURN (ver acceso.json).

Usuarios para entrar desde Internet (con login):
  python3 server.py clave USUARIO [guest|admin] [CONTRASEÑA]
                                                    crea uno (sin contraseña, se inventa una)
  python3 server.py claves                          lista los que hay
  python3 server.py quitar USUARIO                  lo borra
"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web, WSMsgType
from gpiozero import Buzzer, DigitalOutputDevice, Motor

# ---------------------------------------------------------------- Ajustes
PORT = 8080
WATCHDOG_S = 0.5          # parar motores si no llegan órdenes en este tiempo
MANDO_S = 1.0             # con varias pantallas abiertas, conduce la que se movió por última vez;
                          # las demás se ignoran hasta que pase este tiempo sin que la primera se mueva
MAX_SPEED = 1.0           # motores TT de 3-12 V: a 7,4-8,4 V van holgados
DEADZONE = 0.08           # zona muerta del joystick

# Rampas para evitar picos de corriente (y bajadas de tensión en la Pi)
RAMPA_SUBIDA = 2.0        # cuánto puede acelerar por segundo (1.0 = de 0 a tope en 1 s)
RAMPA_BAJADA = 5.0        # cuánto puede frenar por segundo (más rápido que acelerar)
PAUSA_INVERSION = 0.15    # segundos parado antes de cambiar de sentido
CONTROL_HZ = 50           # frecuencia del bucle de control

# Motores (BCM). Si un lado va al revés, cambia su INVERT a True.
PIN_STBY = 16
MOTOR_IZQ = dict(forward=5, backward=6, enable=22)     # Motor A
MOTOR_DER = dict(forward=20, backward=21, enable=26)   # Motor B
INVERT_IZQ = False
INVERT_DER = False
INTERCAMBIAR_LADOS = False  # True si el coche gira al revés de lo que mandas
# Compensación para ir recto (los motores TT nunca son idénticos).
# Si el coche se va a la DERECHA, baja COMPENSA_IZQ; si se va a la IZQUIERDA, baja COMPENSA_DER.
COMPENSA_IZQ = 1.0
COMPENSA_DER = 1.0
# Giro: potencia FIJA de los motores al girar, no depende de la barra de velocidad
# (la barra solo cambia la velocidad de avance). Más bajo = gira más despacio.
GIRO_SITIO = 0.58    # potencia máxima girando sobre sí mismo (sin avanzar)
GIRO_MARCHA = 0.70   # potencia máxima de giro mientras avanza
GIRO_EXPO = 1.8      # curva del joystick: 1 = lineal, >1 = más fino cerca del centro

# Servos (BCM). Límites en grados para no forzar el soporte.
PIN_PAN, PIN_TILT = 12, 13
PAN_MIN, PAN_MAX = -80, 80        # más allá se tensa el cable de la cámara
TILT_MIN, TILT_MAX = -70, 70      # con el cuello actual; bájalo si la cabeza toca
INVERT_PAN = True                 # en este montaje los servos giran al revés
INVERT_TILT = True
PAN_TRIM = 0.0    # grados: corrige el centro del giro (positivo o negativo, de 1 en 1)
TILT_TRIM = 0.0   # grados: corrige la horizontal de la cámara
PULSO_MIN_US, PULSO_MAX_US = 500, 2500  # SG90/MG90 (-90° .. +90°)

# Faros: los dos LED en paralelo en el mismo pin, cada uno con su resistencia (GPIO17 = pin 11)
PIN_FAROS = 17

# Pito: zumbador ACTIVO (TMB12A03) con su transistor NPN (GPIO27 = pin 13).
# Bocina mientras se mantiene pulsado el botón (o la tecla H), con un tope de seguridad.
PIN_PITO = 27
PITO_MAX_S = 3.0             # la bocina se calla sola si se queda "pegada" más de esto
PITO_ARRANQUE = True         # 2 pitidos cortos cuando arranca el servidor
PITO_CONECTAR = True         # 1 pitido corto cuando se conecta una pantalla

# Movimiento de la cámara. La web manda la DIRECCIÓN del joystick ("camvel") y la
# Raspberry calcula la posición en cada vuelta del bucle con su propio reloj, así
# el movimiento es uniforme aunque los mensajes lleguen a ráfagas por la WiFi.
CAM_VEL = 90.0           # grados/segundo con el joystick a tope
CAM_ACEL = 6.0           # cambio de velocidad por segundo (6 = de 0 a tope en ~0,17 s)
CAM_WATCHDOG_S = 0.3     # si dejan de llegar órdenes de cámara, se para
CAM_CENTRAR_VEL = 180.0  # grados/segundo al ir a una posición (botón Centrar)

# Batería: sensor INA219 por I2C (GPIO2 = SDA, GPIO3 = SCL). Sus pines VIN+ y VIN-
# van juntos al positivo de la batería (después del interruptor): solo mide tensión.
# Si el sensor no está conectado, la web simplemente no muestra la batería.
BAT_SENSOR = False          # True si montas el INA219 (opcional)
BAT_I2C_BUS = 1
BAT_I2C_ADDR = 0x40
BAT_CELDAS = 2              # 2S
BAT_LEER_S = 1.0            # cada cuánto se lee
BAT_SUAVE_PARADO = 0.15     # filtro con el coche parado (0-1, más bajo = más estable)
BAT_SUAVE_MARCHA = 0.02     # filtro conduciendo: los tirones de los motores bajan la tensión un momento
BAT_AVISO_PCT = 15          # por debajo de esto, aviso en el registro
# Curva tensión por celda (V) -> % de una 18650 de Li-ion
BAT_CURVA = [(3.30, 0), (3.55, 10), (3.65, 20), (3.70, 30), (3.74, 40), (3.79, 50),
             (3.85, 60), (3.92, 70), (4.00, 80), (4.10, 90), (4.20, 100)]

# Acceso desde Internet (opcional). Sin el archivo acceso.json todo funciona como siempre:
# solo red local o VPN. Con él:
#  - Lo que llega por un proxy inverso (p. ej. el del Synology, con HTTPS) pide usuario y
#    contraseña (pantalla de login). La sesión se guarda en una cookie del navegador, que
#    lleva una firma de la contraseña, no la contraseña.
#    Desde la red local o Tailscale (sin proxy) no hace falta login: se entra como admin.
#  - El vídeo se pide a través de este servidor (/cam/whep). Desde Internet viaja por el
#    TURN de Cloudflare: ni la Pi ni el router abren puertos nuevos.
#  - El archivo se vuelve a leer solo al cambiarlo: no hace falta reiniciar.
ACCESO = Path(__file__).parent / "acceso.json"
MTX_WEBRTC = "http://127.0.0.1:8889"   # WebRTC de MediaMTX en esta misma Pi
MTX_API = "http://127.0.0.1:9997"      # API de MediaMTX (api: yes en mediamtx.yml)
CAM_PATH = "cam"                       # path de la cámara en MediaMTX
VEL_GUEST = 0.6           # velocidad máxima de avance con clave guest (0-1)
LOGIN_MAX_FALLOS = 10     # intentos fallidos de login permitidos...
LOGIN_VENTANA_S = 600     # ...en este tiempo, por cada conexión (luego hay que esperar)
COOKIE = "rc_clave"
TURN_TTL = 48 * 3600      # validez de las credenciales de Cloudflare (máximo 48 h)
TURN_RENOVAR = 24 * 3600  # se renuevan pasado este tiempo si nadie está viendo el vídeo...
TURN_FORZAR = 40 * 3600   # ...y pasado este, aunque alguien lo vea (el vídeo se corta un momento)

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("rccar")


# ---------------------------------------------------------------- Servos
class HwServo:
    """Servo con PWM por hardware (sysfs). Requiere en config.txt:
    dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4  y  dtparam=audio=off
    GPIO12 -> pwm0, GPIO13 -> pwm1."""

    CHIP = Path("/sys/class/pwm/pwmchip0")
    PERIOD_NS = 20_000_000

    def __init__(self, channel):
        self.dir = self.CHIP / f"pwm{channel}"
        if not self.dir.exists():
            (self.CHIP / "export").write_text(str(channel))
        # udev tarda un momento en dar permisos al grupo gpio
        for _ in range(40):
            try:
                (self.dir / "period").write_text(str(self.PERIOD_NS))
                break
            except (PermissionError, FileNotFoundError):
                time.sleep(0.05)
        else:
            raise RuntimeError(f"No se puede usar {self.dir}")
        self._write("enable", 1)

    def _write(self, name, value):
        (self.dir / name).write_text(str(int(value)))

    def set_angle(self, angle):
        us = PULSO_MIN_US + (angle + 90) / 180 * (PULSO_MAX_US - PULSO_MIN_US)
        self._write("duty_cycle", us * 1000)

    def close(self):
        try:
            self._write("enable", 0)
        except OSError:
            pass


class SwServo:
    """Alternativa con PWM por software (tiembla un poco)."""

    def __init__(self, pin):
        from gpiozero import AngularServo
        self.s = AngularServo(pin, min_angle=-90, max_angle=90,
                              min_pulse_width=PULSO_MIN_US / 1e6,
                              max_pulse_width=PULSO_MAX_US / 1e6)

    def set_angle(self, angle):
        self.s.angle = angle

    def close(self):
        self.s.detach()


def crear_servos():
    if HwServo.CHIP.exists():
        try:
            log.info("Servos con PWM por hardware")
            return HwServo(0), HwServo(1)
        except Exception as e:  # noqa: BLE001
            log.warning("PWM por hardware no disponible (%s)", e)
    log.info("Servos con PWM por software")
    return SwServo(PIN_PAN), SwServo(PIN_TILT)


# ---------------------------------------------------------------- Batería
class Bateria:
    """Tensión de la batería con un INA219 (registro de tensión de bus, sin calibrar)."""

    def __init__(self):
        self.v = None
        self.bus = None
        if not BAT_SENSOR:
            log.info("Medida de batería desactivada (BAT_SENSOR = False)")
            return
        try:
            from smbus2 import SMBus
            self.bus = SMBus(BAT_I2C_BUS)
            self._leer_v()
            log.info("Sensor de batería INA219 encontrado")
        except Exception as e:  # noqa: BLE001
            self.bus = None
            log.info("Sin sensor de batería (%s)", e)

    def _leer_v(self):
        hi, lo = self.bus.read_i2c_block_data(BAT_I2C_ADDR, 0x02, 2)
        return (((hi << 8) | lo) >> 3) * 0.004          # 4 mV por unidad

    def leer(self, conduciendo):
        if not self.bus:
            return
        try:
            v = self._leer_v()
        except OSError:
            return
        if self.v is None:
            self.v = v
        else:
            k = BAT_SUAVE_MARCHA if conduciendo else BAT_SUAVE_PARADO
            self.v += (v - self.v) * k

    @property
    def porcentaje(self):
        if self.v is None:
            return None
        vc = self.v / BAT_CELDAS
        if vc <= BAT_CURVA[0][0]:
            return 0
        for (v0, p0), (v1, p1) in zip(BAT_CURVA, BAT_CURVA[1:]):
            if vc <= v1:
                return round(p0 + (vc - v0) / (v1 - v0) * (p1 - p0))
        return 100

    def estado(self):
        if self.v is None:
            return None
        return {"t": "bat", "v": round(self.v, 2), "pct": self.porcentaje}


# ---------------------------------------------------------------- Coche
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def num(v, default=0.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


class Coche:
    def __init__(self):
        self.stby = DigitalOutputDevice(PIN_STBY, initial_value=True)
        self.faros = DigitalOutputDevice(PIN_FAROS, initial_value=False)
        self.pito = Buzzer(PIN_PITO)
        self.bocina_desde = None         # cuándo se pulsó la bocina (None = suelta)
        self.izq = Motor(**MOTOR_IZQ)
        self.der = Motor(**MOTOR_DER)
        self.pan_s, self.tilt_s = crear_servos()
        self.pan = self.tilt = 0.0       # posición actual de la cámara
        self.cam_vel = [0.0, 0.0]        # velocidad pedida (-1..1): pan, tilt
        self.cam_vel_act = [0.0, 0.0]    # velocidad aplicada (con aceleración)
        self.cam_t = 0.0                 # cuándo llegó la última orden de velocidad
        self.cam_destino = None          # (pan, tilt) si hay que ir a una posición
        self.cam_escrito = (0.0, 0.0)    # última posición mandada a los servos
        self.objetivo = [0.0, 0.0]   # velocidad pedida (izq, der)
        self.actual = [0.0, 0.0]     # velocidad aplicada (izq, der)
        self.pausa = [0.0, 0.0]      # hasta cuándo esperar parado antes de invertir
        self.ultima_orden = 0.0
        self._mover_servos(0, 0)

    @property
    def en_marcha(self):
        return any(self.objetivo) or any(self.actual)

    def conducir(self, x, y, vel):
        """x: giro (-1 izq .. 1 der), y: avance (-1 atrás .. 1 adelante)."""
        x, y = clamp(num(x), -1, 1), clamp(num(y), -1, 1)
        vel = clamp(num(vel, 1.0), 0, 1) * MAX_SPEED
        if abs(x) < DEADZONE:
            x = 0.0
        if abs(y) < DEADZONE:
            y = 0.0
        # Giro más suave: curva (fino cerca del centro) y fuerza máxima limitada,
        # algo menor girando sobre sí mismo que avanzando.
        giro = GIRO_SITIO + (GIRO_MARCHA - GIRO_SITIO) * abs(y)
        x = math.copysign(abs(x) ** GIRO_EXPO, x) * giro   # potencia de giro fija
        y = y * vel                                        # la barra solo afecta al avance
        izq, der = y + x, y - x
        m = max(MAX_SPEED, abs(izq), abs(der)) / MAX_SPEED  # sin pasar de MAX_SPEED
        self.objetivo = [izq / m, der / m]
        self.ultima_orden = time.monotonic()

    def paso(self, dt):
        """Acerca la velocidad aplicada a la pedida, sin saltos bruscos."""
        ahora = time.monotonic()
        for i in (0, 1):
            obj, act = self.objetivo[i], self.actual[i]
            # Cambio de sentido: primero frenar hasta 0
            if act and obj and (act > 0) != (obj > 0):
                obj = 0.0
            # Recién parado tras un cambio de sentido: esperar un momento
            if ahora < self.pausa[i]:
                obj = 0.0
            acelerando = abs(obj) > abs(act)
            lim = (RAMPA_SUBIDA if acelerando else RAMPA_BAJADA) * dt
            nuevo = act + clamp(obj - act, -lim, lim)
            if abs(nuevo) < 0.01:
                nuevo = 0.0
            if act and not nuevo:
                self.pausa[i] = ahora + PAUSA_INVERSION
            self.actual[i] = nuevo
        self._aplicar()

    def _aplicar(self):
        izq, der = self.actual
        izq *= COMPENSA_IZQ
        der *= COMPENSA_DER
        if INTERCAMBIAR_LADOS:
            izq, der = der, izq
        self.izq.value = -izq if INVERT_IZQ else izq
        self.der.value = -der if INVERT_DER else der

    def parar(self):
        """Parada inmediata (los motores quedan libres, sin pico de corriente)."""
        self.objetivo = [0.0, 0.0]
        self.actual = [0.0, 0.0]
        self.izq.stop()
        self.der.stop()

    def camara(self, pan, tilt):
        """Ir a una posición concreta (por ejemplo, centrar)."""
        self.cam_destino = (clamp(num(pan), PAN_MIN, PAN_MAX),
                            clamp(num(tilt), TILT_MIN, TILT_MAX))
        self.cam_vel = [0.0, 0.0]
        self.cam_vel_act = [0.0, 0.0]

    def camara_vel(self, x, y):
        """Joystick de la cámara: x (-1 izquierda .. 1 derecha), y (-1 abajo .. 1 arriba)."""
        x, y = clamp(num(x), -1, 1), clamp(num(y), -1, 1)
        self.cam_vel = [0.0 if abs(x) < DEADZONE else x,
                        0.0 if abs(y) < DEADZONE else y]
        self.cam_t = time.monotonic()
        if any(self.cam_vel):
            self.cam_destino = None

    def paso_camara(self, dt):
        """Avanza la cámara un paso de tiempo (se llama 50 veces por segundo)."""
        if time.monotonic() - self.cam_t > CAM_WATCHDOG_S:
            self.cam_vel = [0.0, 0.0]
        lim = CAM_ACEL * dt
        for i in (0, 1):
            self.cam_vel_act[i] += clamp(self.cam_vel[i] - self.cam_vel_act[i], -lim, lim)
        pan = self.pan + self.cam_vel_act[0] * CAM_VEL * dt
        tilt = self.tilt + self.cam_vel_act[1] * CAM_VEL * dt
        if self.cam_destino is not None:
            paso = CAM_CENTRAR_VEL * dt
            pan += clamp(self.cam_destino[0] - pan, -paso, paso)
            tilt += clamp(self.cam_destino[1] - tilt, -paso, paso)
            if (pan, tilt) == self.cam_destino:
                self.cam_destino = None
        self.pan = clamp(pan, PAN_MIN, PAN_MAX)
        self.tilt = clamp(tilt, TILT_MIN, TILT_MAX)
        # En el tope, quitar la velocidad que empuja contra él (así cambia de sentido al momento)
        for i, (v, lo, hi) in enumerate(((self.pan, PAN_MIN, PAN_MAX),
                                         (self.tilt, TILT_MIN, TILT_MAX))):
            if (v == lo and self.cam_vel_act[i] < 0) or (v == hi and self.cam_vel_act[i] > 0):
                self.cam_vel_act[i] = 0.0
        pos = (round(self.pan, 2), round(self.tilt, 2))
        if pos != self.cam_escrito:
            self.cam_escrito = pos
            self._mover_servos(self.pan, self.tilt)

    def _mover_servos(self, pan, tilt):
        p = -pan if INVERT_PAN else pan
        t = -tilt if INVERT_TILT else tilt
        self.pan_s.set_angle(clamp(p + PAN_TRIM, -90, 90))
        self.tilt_s.set_angle(clamp(t + TILT_TRIM, -90, 90))

    def bocina(self, on):
        if on:
            self.bocina_desde = time.monotonic()
            self.pito.on()                 # corta cualquier pitido en curso
        elif self.bocina_desde is not None:
            self.bocina_desde = None
            self.pito.off()

    def pitar(self, n=1, on_s=0.08, off_s=0.12):
        """Pitidos cortos en segundo plano (no se pisan con la bocina)."""
        if self.bocina_desde is None:
            self.pito.beep(on_time=on_s, off_time=off_s, n=n, background=True)

    def cerrar(self):
        self.parar()
        self.stby.off()
        self.faros.off()
        self.pito.off()
        self.pan_s.close()
        self.tilt_s.close()


# ---------------------------------------------------------------- Acceso desde Internet
def rol_valido(rol):
    """admin o guest (también acepta los nombres antiguos dueno / invitado)."""
    return "admin" if rol in ("admin", "dueno") else "guest"


class Acceso:
    """Lee acceso.json (cuando cambia) y decide quién entra.

    {
      "dominio": "coche.ejemplo.com",
      "proxy_ips": ["192.168.1.10"],
      "claves": {"CONTRASEÑA": {"nombre": "amigo", "rol": "guest"}},
      "secreto": "...(lo crea solo, para firmar las sesiones)...",
      "cloudflare_turn": {"key_id": "...", "api_token": "..."}
    }
    """

    def __init__(self):
        self.cfg, self._mtime = {}, None

    def datos(self):
        try:
            m = ACCESO.stat().st_mtime
        except FileNotFoundError:
            self.cfg, self._mtime = {}, None
            return self.cfg
        if m != self._mtime:
            self._mtime = m
            try:
                self.cfg = json.loads(ACCESO.read_text(encoding="utf-8")) or {}
                log.info("acceso.json leído: %d claves", len(self.cfg.get("claves", {})))
            except (OSError, ValueError) as e:
                log.error("acceso.json no válido (%s): desde Internet no entra nadie", e)
                self.cfg = {"claves": {}}
        return self.cfg

    def es_publico(self, request):
        """True si la petición llega por el proxy inverso (desde Internet)."""
        cfg = self.datos()
        if not cfg:
            return False                      # sin acceso.json: como siempre
        h = request.headers
        if "X-Forwarded-For" in h or "X-Real-IP" in h or "Forwarded" in h:
            return True
        return request.remote in cfg.get("proxy_ips", [])

    @staticmethod
    def es_https(request):
        return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def recargar(self):
        """Volver a leer acceso.json la próxima vez (después de escribirlo)."""
        self._mtime = None

    def clave(self, request):
        """La clave (contraseña) de la sesión, solo desde Internet. La cookie no lleva la
        contraseña sino su firma: se busca qué clave la produce."""
        if not self.es_publico(request):
            return None
        token = request.cookies.get(COOKIE)
        cfg = self.datos()
        secreto = cfg.get("secreto")
        if not token or not secreto:
            return None
        for c in cfg.get("claves", {}):
            if hmac.compare_digest(token, firma(secreto, c)):
                return c
        return None

    def comprobar(self, usuario, contrasena):
        """La clave si usuario y contraseña son correctos; si no, None."""
        info = self.datos().get("claves", {}).get(contrasena)
        if isinstance(info, dict) and usuario.strip().casefold() == str(info.get("nombre", "")).casefold():
            return contrasena
        return None

    def quien(self, request):
        """(rol, nombre): rol "admin" o "guest"; (None, None) si no puede entrar."""
        if not self.es_publico(request):
            return "admin", "en casa"
        clave = self.clave(request)
        info = self.datos().get("claves", {}).get(clave) if clave else None
        if not isinstance(info, dict):
            return None, None
        return rol_valido(info.get("rol")), info.get("nombre", "?")


def firma(secreto, clave):
    return hmac.new(secreto.encode(), clave.encode(), hashlib.sha256).hexdigest()


def ip_cliente(request):
    xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return xff or request.headers.get("X-Real-IP") or request.remote or "?"


class Turn:
    """Credenciales del TURN de Cloudflare: las pide, las renueva y se las pasa a MediaMTX."""

    def __init__(self, app):
        self.app = app
        self.servers = []      # formato del navegador (RTCIceServer)
        self.t = 0.0           # cuándo se generaron (time.time)
        self._ultimo_aviso = None

    def para_navegador(self):
        return self.servers

    def _aviso(self, msg):
        if msg != self._ultimo_aviso:          # no repetir el mismo aviso cada minuto
            self._ultimo_aviso = msg
            if msg:
                log.warning("TURN: %s", msg)

    async def bucle(self):
        while True:
            try:
                await self.revisar()
                self._aviso(None)
            except Exception as e:             # noqa: BLE001 - que nunca pare el bucle
                self._aviso(str(e) or type(e).__name__)
            await asyncio.sleep(60)

    async def revisar(self):
        cfg = self.app["acceso"].datos().get("cloudflare_turn") or {}
        if not cfg.get("key_id") or not cfg.get("api_token"):
            if self.servers:
                self.servers, self.t = [], 0.0
                await self.aplicar_mediamtx()
            return
        edad = time.time() - self.t
        if not self.servers or edad > TURN_FORZAR or (edad > TURN_RENOVAR and not await self.hay_video()):
            await self.generar(cfg)
        await self.aplicar_mediamtx()

    async def generar(self, cfg):
        url = (f"https://rtc.live.cloudflare.com/v1/turn/keys/{cfg['key_id']}"
               "/credentials/generate-ice-servers")
        async with self.app["http"].post(
                url, json={"ttl": TURN_TTL},
                headers={"Authorization": f"Bearer {cfg['api_token']}"},
                timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status >= 300:
                raise RuntimeError(f"Cloudflare respondió {r.status}: {(await r.text())[:200]}")
            d = await r.json()
        servers = d.get("iceServers") or []
        if isinstance(servers, dict):
            servers = [servers]
        limpio = []
        for s in servers:
            urls = s.get("urls") or []
            urls = [urls] if isinstance(urls, str) else list(urls)
            urls = [u for u in urls if not re.search(r":53(\?|$)", u)]   # el 53 lo bloquean los navegadores
            if urls:
                limpio.append({**s, "urls": urls})
        if not limpio:
            raise RuntimeError("Cloudflare no devolvió servidores")
        self.servers, self.t = limpio, time.time()
        log.info("TURN: credenciales nuevas de Cloudflare (%d h)", TURN_TTL // 3600)

    def para_mediamtx(self):
        """Para la Pi basta STUN + TURN por UDP 3478 + TURNS por 443 (por si el UDP falla)."""
        res = []
        for s in self.servers:
            for u in s["urls"]:
                if u.startswith("stun:") and ":3478" in u:
                    res.append({"url": u, "username": "", "password": "", "clientOnly": False})
                elif (u.startswith("turn:") and ":3478" in u and "udp" in u) or \
                        (u.startswith("turns:") and ":443" in u):
                    res.append({"url": u, "username": s.get("username", ""),
                                "password": s.get("credential", ""), "clientOnly": False})
        return res

    async def aplicar_mediamtx(self):
        """Pone los servidores en MediaMTX si no los tiene (p. ej. después de reiniciarse)."""
        http = self.app["http"]
        quiero = self.para_mediamtx()
        tmo = aiohttp.ClientTimeout(total=5)
        try:
            async with http.get(f"{MTX_API}/v3/config/global/get", timeout=tmo) as r:
                r.raise_for_status()
                actual = (await r.json()).get("webrtcICEServers2") or []
        except aiohttp.ClientError as e:
            raise RuntimeError(f"no se puede usar la API de MediaMTX en {MTX_API} "
                               f"(¿api: yes en mediamtx.yml?): {e}") from None
        clave = lambda lista: [(x.get("url"), x.get("username")) for x in lista]  # noqa: E731
        if clave(actual) == clave(quiero):
            return
        async with http.patch(f"{MTX_API}/v3/config/global/patch",
                              json={"webrtcICEServers2": quiero}, timeout=tmo) as r:
            if r.status >= 300:
                raise RuntimeError(f"MediaMTX no aceptó los servidores TURN: {r.status} {(await r.text())[:200]}")
        log.info("TURN: MediaMTX actualizado (%d servidores)", len(quiero))

    async def hay_video(self):
        try:
            async with self.app["http"].get(f"{MTX_API}/v3/webrtcsessions/list",
                                            timeout=aiohttp.ClientTimeout(total=5)) as r:
                return (await r.json()).get("itemCount", 1) > 0
        except (aiohttp.ClientError, ValueError):
            return True


# ---------------------------------------------------------------- Web
async def index(request):
    acceso: Acceso = request.app["acceso"]
    rol, _ = acceso.quien(request)
    pagina = "index.html" if rol else "login.html"    # sin sesión: pantalla de login
    return web.FileResponse(STATIC / pagina, headers={"Cache-Control": "no-cache"})


def asegurar_secreto(acceso):
    """El secreto con el que se firman las sesiones (se crea la primera vez)."""
    secreto = acceso.datos().get("secreto")
    if secreto:
        return secreto
    import secrets
    datos = leer_archivo_acceso()
    datos["secreto"] = secrets.token_hex(32)
    guardar_archivo_acceso(datos)
    acceso.recargar()
    return datos["secreto"]


async def login(request):
    app = request.app
    acceso: Acceso = app["acceso"]
    ip = ip_cliente(request)
    ahora = time.monotonic()
    fallos = [t for t in app["fallos"].get(ip, []) if ahora - t < LOGIN_VENTANA_S]
    app["fallos"][ip] = fallos
    if len(fallos) >= LOGIN_MAX_FALLOS:
        return web.json_response({"ok": False, "error": "Demasiados intentos. Espera unos minutos."}, status=429)
    try:
        d = await request.json()
    except ValueError:
        d = {}
    usuario, contrasena = str(d.get("usuario", ""))[:64], str(d.get("clave", ""))[:128]
    clave = acceso.comprobar(usuario, contrasena)
    if clave is None:
        fallos.append(ahora)
        log.warning("Login fallido desde %s (usuario '%s')", ip, usuario[:30])
        await asyncio.sleep(0.6)
        return web.json_response({"ok": False, "error": "Usuario o contraseña incorrectos"}, status=401)
    try:
        secreto = asegurar_secreto(acceso)
    except (OSError, ValueError) as e:
        log.error("No se puede guardar el secreto de sesión: %s", e)
        return web.json_response({"ok": False, "error": "Error en el coche (acceso.json)"}, status=500)
    app["fallos"].pop(ip, None)
    log.info("Login de '%s' desde %s", acceso.datos()["claves"][clave].get("nombre"), ip)
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, firma(secreto, clave), max_age=365 * 86400, httponly=True,
                    samesite="Lax", secure=Acceso.es_https(request))
    return resp


async def logout(request):
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE)
    return resp


async def ice(request):
    """Servidores ICE para el vídeo: TURN solo para quien entra desde Internet."""
    acceso: Acceso = request.app["acceso"]
    if acceso.quien(request)[0] is None:
        raise web.HTTPForbidden()
    servers = request.app["turn"].para_navegador() if acceso.es_publico(request) else []
    return web.json_response({"iceServers": servers}, headers={"Cache-Control": "no-store"})


async def whep(request):
    """Pasa la conexión del vídeo (WHEP) a MediaMTX: así todo va por una sola dirección."""
    if request.app["acceso"].quien(request)[0] is None:
        raise web.HTTPForbidden()
    headers = {h: request.headers[h] for h in ("Content-Type", "If-Match") if h in request.headers}
    try:
        async with request.app["http"].request(
                request.method, MTX_WEBRTC + request.path_qs, data=await request.read(),
                headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            body = await r.read()
            out = {h: r.headers[h] for h in ("Content-Type", "Location", "ETag", "Link", "Accept-Patch")
                   if h in r.headers}
            return web.Response(status=r.status, body=body, headers=out)
    except aiohttp.ClientError as e:
        log.warning("Vídeo: MediaMTX no responde (%s)", e)
        raise web.HTTPBadGateway() from None


# ---------------------------------------------------------------- Archivo de claves
def leer_archivo_acceso():
    """Lee acceso.json para modificarlo (ValueError si está mal escrito)."""
    if ACCESO.exists():
        return json.loads(ACCESO.read_text(encoding="utf-8"))
    return {"dominio": "", "proxy_ips": [], "claves": {},
            "cloudflare_turn": {"key_id": "", "api_token": ""}}


def guardar_archivo_acceso(datos):
    for i in datos.setdefault("claves", {}).values():   # nombres antiguos (dueno/invitado) -> admin/guest
        i["rol"] = rol_valido(i.get("rol"))
    ACCESO.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ACCESO.chmod(0o600)


def contrasena_aleatoria():
    """Fácil de teclear en el móvil: sin mayúsculas ni letras que se confunden (l/1, o/0)."""
    import secrets
    letras = "abcdefghjkmnpqrstuvwxyz23456789"
    t = "".join(secrets.choice(letras) for _ in range(10))
    return f"{t[:5]}-{t[5:]}"


def crear_clave(datos, nombre, rol, clave=None):
    """Añade un usuario a datos. Devuelve (contraseña, None) o (None, error)."""
    nombre = (nombre or "").strip()[:30]
    if not nombre:
        return None, "Pon un nombre de usuario"
    claves = datos.setdefault("claves", {})
    if any(str(i.get("nombre", "")).casefold() == nombre.casefold() for i in claves.values()):
        return None, f"Ya hay un usuario llamado '{nombre}'"
    clave = (clave or "").strip() or contrasena_aleatoria()
    if not re.fullmatch(r"\S{8,64}", clave):
        return None, "La contraseña tiene que tener entre 8 y 64 caracteres, sin espacios"
    if clave in claves:
        return None, "Esa contraseña ya la usa otro usuario: elige otra"
    claves[clave] = {"nombre": nombre, "rol": rol_valido(rol)}
    return clave, None


def direccion(datos, request):
    dominio = datos.get("dominio")
    if dominio:
        return f"https://{dominio}/"
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{'https' if Acceso.es_https(request) else 'http'}://{host}/"


def credenciales(datos, request, nombre, clave):
    return (f"Para conducir mi coche entra en {direccion(datos, request)}\n"
            f"Usuario: {nombre}\nContraseña: {clave}")


# ---------------------------------------------------------------- Pantallas conectadas
# app["clientes"]: {ws: {"id", "rol", "nombre", "clave"}}
# app["bloqueo"]: pantalla de admin que ha "tomado el control" (los guest solo miran) o None
ACCIONES_ADMIN = ("control", "echar", "invitar", "claves", "borrar_clave")


def conductor_actual(app):
    """La pantalla que está conduciendo ahora mismo, o None."""
    if app["mando"] is not None and time.monotonic() - app["mando_t"] < MANDO_S:
        return app["mando"]
    return None


def enviar_estado(app):
    """Manda a cada pantalla quién tiene el control y, a los admin, quién está conectado.
    A cada una solo se le manda si ha cambiado algo para ella."""
    clientes = app["clientes"]
    if app["bloqueo"] is not None and not any(i["rol"] == "guest" for i in clientes.values()):
        app["bloqueo"] = None                 # ya no queda ningún guest: se suelta el control
    conductor = conductor_actual(app)
    gente = [{"id": i["id"], "nombre": i["nombre"], "rol": i["rol"], "conduce": w is conductor}
             for w, i in clientes.items()]
    bloq = clientes.get(app["bloqueo"]) if app["bloqueo"] is not None else None
    for w, i in list(clientes.items()):
        msg = {"t": "estado", "control": bloq["nombre"] if bloq else None, "mio": w is app["bloqueo"]}
        if i["rol"] == "admin":
            msg["gente"] = gente
        txt = json.dumps(msg)
        if txt != i.get("estado"):
            i["estado"] = txt
            asyncio.ensure_future(w.send_str(txt))


async def echar(objetivo, motivo):
    try:
        await objetivo.send_json({"t": "echado", "motivo": motivo})
    except (ConnectionError, RuntimeError):
        pass
    await objetivo.close()


def lista_claves(app, request):
    datos = app["acceso"].datos()
    return [{"nombre": i.get("nombre", "?"), "clave": c,
             "texto": credenciales(datos, request, i.get("nombre", "?"), c)}
            for c, i in datos.get("claves", {}).items() if rol_valido(i.get("rol")) == "guest"]


async def borrar_clave(app, clave):
    """Borra una clave de guest y echa a quien esté conectado con ella."""
    datos = leer_archivo_acceso()
    info = datos.get("claves", {}).get(clave)
    if not info or rol_valido(info.get("rol")) != "guest":
        return
    del datos["claves"][clave]
    guardar_archivo_acceso(datos)
    app["acceso"].recargar()
    log.info("Clave de '%s' borrada desde la web", info.get("nombre"))
    for w, i in list(app["clientes"].items()):
        if i.get("clave") == clave:
            await echar(w, "Tu acceso ha sido retirado.")


async def accion_admin(app, ws, request, d):
    t = d.get("t")
    coche: Coche = app["coche"]
    if t == "control":
        if d.get("tomar"):                    # el admin conduce; los guest solo miran
            app["bloqueo"] = ws
            app["mando"], app["mando_t"], app["mando_rol"] = ws, time.monotonic(), "admin"
            coche.parar()
            coche.camara_vel(0, 0)
        elif app["bloqueo"] is ws:
            app["bloqueo"] = app["mando"] = None
        enviar_estado(app)
        return
    if t == "echar":
        objetivo = next((w for w, i in app["clientes"].items() if i["id"] == d.get("id")), None)
        if objetivo is None or app["clientes"][objetivo]["rol"] != "guest":
            return                            # a un admin no se le puede echar
        info = app["clientes"][objetivo]
        if d.get("quitar") and info.get("clave"):
            await borrar_clave(app, info["clave"])
            await ws.send_json({"t": "claves", "lista": lista_claves(app, request)})   # refrescar el panel
        else:
            log.info("'%s' echado desde la web", info["nombre"])
            await echar(objetivo, "El admin te ha desconectado.")
        return
    if t == "invitar":
        datos = leer_archivo_acceso()
        clave, error = crear_clave(datos, d.get("nombre"), "guest", d.get("clave"))
        if error:
            await ws.send_json({"t": "aviso", "msg": error})
            return
        guardar_archivo_acceso(datos)
        app["acceso"].recargar()
        log.info("Invitado '%s' creado desde la web", datos["claves"][clave]["nombre"])
        nombre = datos["claves"][clave]["nombre"]
        await ws.send_json({"t": "invitado", "nombre": nombre,
                            "texto": credenciales(datos, request, nombre, clave),
                            "sin_dominio": not datos.get("dominio")})
    elif t == "borrar_clave":
        await borrar_clave(app, d.get("clave"))
    await ws.send_json({"t": "claves", "lista": lista_claves(app, request)})


async def ws_handler(request):
    app = request.app
    coche: Coche = app["coche"]
    acceso: Acceso = app["acceso"]
    rol, nombre = acceso.quien(request)
    if rol is None:
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=5)
    await ws.prepare(request)
    app["sig_id"] += 1
    mi_id = app["sig_id"]
    app["clientes"][ws] = {"id": mi_id, "rol": rol, "nombre": nombre, "clave": acceso.clave(request)}
    log.info("Cliente conectado (%s, %s, %s)", request.remote, nombre, rol)
    if PITO_CONECTAR:
        coche.pitar(1)
    await ws.send_json({"t": "hola", "pan": coche.pan, "tilt": coche.tilt,
                        "faros": coche.faros.is_active,
                        "limites": {"pan": [PAN_MIN, PAN_MAX],
                                    "tilt": [TILT_MIN, TILT_MAX]},
                        "rol": rol, "id": mi_id, "nombre": nombre,
                        "sesion": acceso.es_publico(request)})
    if app["bat"].estado():
        await ws.send_json(app["bat"].estado())
    enviar_estado(app)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            t = d.get("t")
            if t in ACCIONES_ADMIN:
                if rol == "admin":
                    try:
                        await accion_admin(app, ws, request, d)
                    except (OSError, ValueError) as e:
                        await ws.send_json({"t": "aviso", "msg": f"No se ha podido: {e}"})
                continue
            if rol == "guest" and app["bloqueo"] is not None:
                continue          # el admin ha tomado el control: el guest solo mira
            if t == "drive":
                ahora = time.monotonic()
                conductor = app["mando"]
                mueve = bool(d.get("x") or d.get("y"))
                # un admin le quita el mando a un guest, pero solo cuando de verdad conduce
                # (sus "quieto" no deben frenar al guest)
                quita = rol == "admin" and app["mando_rol"] == "guest" and mueve
                if conductor is not None and conductor is not ws and ahora - app["mando_t"] < MANDO_S \
                        and not quita:
                    continue          # otra pantalla está conduciendo: ignorar esta
                if mueve:
                    app["mando"], app["mando_t"], app["mando_rol"] = ws, ahora, rol
                v = num(d.get("v", 1), 1.0)
                if rol == "guest":
                    v = min(v, VEL_GUEST)
                coche.conducir(d.get("x"), d.get("y"), v)
            elif t == "camvel":
                coche.camara_vel(d.get("x"), d.get("y"))
            elif t == "cam":
                coche.camara(d.get("pan"), d.get("tilt"))
            elif t == "stop":
                # un guest no puede frenar a un admin que conduce; el resto, sí
                conductor = conductor_actual(app)
                if rol == "admin" or conductor in (None, ws) or app["mando_rol"] == "guest":
                    coche.parar()
            elif t == "faros":
                coche.faros.value = bool(d.get("on"))
                # avisar a todas las pantallas abiertas para que el botón muestre lo mismo
                estado = {"t": "faros", "on": coche.faros.is_active}
                for c in list(app["clientes"]):
                    asyncio.ensure_future(c.send_json(estado))
            elif t == "pito":
                coche.bocina(bool(d.get("on")))
    finally:
        app["clientes"].pop(ws, None)
        if app["bloqueo"] is ws:
            app["bloqueo"] = None
        conducia = app["mando"] is ws
        if conducia:
            app["mando"] = None
        coche.bocina(False)
        # solo se para si conducía esta pantalla (o ya no queda nadie): cerrar otra no estorba
        if conducia or not app["clientes"]:
            coche.parar()
            coche.camara_vel(0, 0)
            log.info("Cliente desconectado (%s), motores parados", nombre)
        else:
            log.info("Cliente desconectado (%s)", nombre)
        enviar_estado(app)
    return ws


async def control(app):
    """Bucle de control: cámara, rampas de velocidad y watchdog."""
    coche: Coche = app["coche"]
    bat: Bateria = app["bat"]
    t_bat = 0.0
    avisado = False
    t_prev = time.monotonic()
    vuelta = 0
    ultima_pos = None
    while True:
        await asyncio.sleep(1 / CONTROL_HZ)
        ahora = time.monotonic()
        dt, t_prev = min(ahora - t_prev, 0.1), ahora   # tiempo real transcurrido
        coche.paso_camara(dt)
        if ahora - t_bat >= BAT_LEER_S:       # batería: leer y mandar a la web
            t_bat = ahora
            bat.leer(coche.en_marcha)
            est = bat.estado()
            if est:
                for ws in list(app["clientes"]):
                    asyncio.ensure_future(ws.send_json(est))
                if est["pct"] < BAT_AVISO_PCT and not avisado:
                    log.warning("Batería baja: %s %% (%.2f V)", est["pct"], est["v"])
                avisado = est["pct"] < BAT_AVISO_PCT
        if coche.bocina_desde is not None and ahora - coche.bocina_desde > PITO_MAX_S:
            coche.bocina(False)               # bocina pegada (se perdió el "soltar")
        vuelta += 1
        if vuelta % 5 == 0:   # 10 veces por segundo: posición de la cámara para la web
            pos = (round(coche.pan, 1), round(coche.tilt, 1))
            if pos != ultima_pos:
                ultima_pos = pos
                for ws in list(app["clientes"]):
                    asyncio.ensure_future(ws.send_json({"t": "pos", "pan": pos[0], "tilt": pos[1]}))
        if vuelta % 10 == 0:  # 5 veces por segundo: quién conduce / quién está (si ha cambiado)
            enviar_estado(app)
        if coche.en_marcha and ahora - coche.ultima_orden > WATCHDOG_S:
            coche.parar()
            log.warning("Watchdog: sin órdenes, motores parados")
            continue
        if coche.en_marcha:
            coche.paso(dt)


async def on_startup(app):
    app["coche"] = Coche()
    app["bat"] = Bateria()
    app["clientes"] = {}
    app["sig_id"] = 0
    app["bloqueo"] = None
    app["fallos"] = {}       # intentos de login fallidos por conexión
    app["mando"], app["mando_t"], app["mando_rol"] = None, 0.0, None   # pantalla que está conduciendo
    app["acceso"] = Acceso()
    app["http"] = aiohttp.ClientSession()
    app["turn"] = Turn(app)
    app["turn_task"] = asyncio.create_task(app["turn"].bucle())
    app["wd"] = asyncio.create_task(control(app))
    if PITO_ARRANQUE:
        app["coche"].pitar(2)


async def on_cleanup(app):
    app["wd"].cancel()
    app["turn_task"].cancel()
    await app["http"].close()
    app["coche"].cerrar()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/ice", ice)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_route("*", f"/{CAM_PATH}/whep", whep)
    app.router.add_route("*", f"/{CAM_PATH}/whep/{{sesion}}", whep)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, port=PORT)


# ---------------------------------------------------------------- Claves (línea de órdenes)
def cli_claves(args):
    try:
        datos = leer_archivo_acceso()
    except ValueError as e:
        print(f"{ACCESO.name} tiene un error en la línea {getattr(e, 'lineno', '?')}, "
              f"columna {getattr(e, 'colno', '?')}: {getattr(e, 'msg', e)}")
        print("Revisa que los textos y las IP vayan entre comillas y que haya comas entre líneas.")
        return
    claves = datos.setdefault("claves", {})
    orden = args[0]
    if orden == "clave":
        nombre = args[1] if len(args) > 1 else "guest"
        rol = rol_valido(args[2] if len(args) > 2 else "guest")
        clave, error = crear_clave(datos, nombre, rol, args[3] if len(args) > 3 else None)
        if error:
            print(error)
            return
        print(f"Usuario {rol} creado:")
        print(f"  Dirección:  https://{datos.get('dominio') or 'TU-DOMINIO'}/")
        print(f"  Usuario:    {nombre}")
        print(f"  Contraseña: {clave}")
    elif orden == "quitar" and len(args) > 1:
        borrar = [c for c, i in claves.items() if str(i.get("nombre", "")).casefold() == args[1].casefold()]
        for c in borrar:
            del claves[c]
        print(f"Quitado el usuario '{args[1]}'" if borrar else f"No hay ningún usuario '{args[1]}'")
    else:
        for c, i in claves.items():
            print(f"{i.get('nombre', '?'):15} {rol_valido(i.get('rol')):6} {c}")
        if not claves:
            print("No hay usuarios")
        return
    guardar_archivo_acceso(datos)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("clave", "claves", "quitar"):
        cli_claves(sys.argv[1:])
    else:
        main()
