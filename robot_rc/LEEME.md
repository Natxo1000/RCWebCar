# Robot RC – software de control

Servidor en Python para la Raspberry Pi y una web de control. Desde el navegador del móvil o del PC puedes:

- conducir,
- mover la cámara,
- ver el vídeo en directo,
- encender los faros,
- tocar el pito.

Código, novedades y dudas: **https://github.com/Natxo1000/RCWebCar**

| Archivo | Qué es |
|---|---|
| `server.py` | Servidor (aiohttp). Motores, servos, faros, pito y watchdog de seguridad |
| `static/index.html` | Web de control: joysticks en el móvil, teclado en el PC, vídeo WebRTC |
| `static/login.html` | Pantalla de login (solo para quien entra desde Internet) |
| `robot_rc.service` | Servicio de systemd para que arranque solo al encender |

## 1. Conexiones (numeración BCM)

| Función | GPIO | Pin físico | Notas |
|---|---|---|---|
| TB6612 AIN1 / AIN2 / PWMA (motores izquierdos) | 5 / 6 / 22 | 29 / 31 / 15 | Los 2 motores de cada lado en paralelo en la misma salida |
| TB6612 BIN1 / BIN2 / PWMB (motores derechos) | 20 / 21 / 26 | 38 / 40 / 37 | |
| TB6612 STBY | 16 | 36 | |
| Servo de giro (pan), señal | 12 | 32 | PWM por hardware |
| Servo de inclinación (tilt), señal | 13 | 33 | PWM por hardware |
| Faros (base del transistor, con 1 kΩ) | 17 | 11 | Cada LED con su resistencia de 100 Ω, desde el 5 V |
| Pito (base del transistor, con 1 kΩ) | 27 | 13 | Zumbador **activo** de 3 V entre el 3,3 V y el colector |
| INA219 SDA / SCL (opcional) | 2 / 3 | 3 / 5 | Medida de batería. Desactivado por defecto |

- La perfboard lleva **dos reguladores MP1584, los dos ajustados a 5 V** (ajústalos con el polímetro antes de conectar nada): uno alimenta la Pi y el otro **solo los servos**, con un condensador electrolítico en su salida (de 470 a 2200 µF, de 10 V o más; respeta la polaridad: la franja va al GND). Así los picos de los servos no reinician la Pi. No alimentes los servos desde el pin de 5 V de la Pi.
- Todos los GND van unidos.
- **Batería:** celdas 18650 en **2S o 2S2P** (7,4 V, 8,4 V cargada) con BMS. Con 1S no llega tensión y con 3S se queman los motores TT y el TB6612.

## 2. Instalación en la Raspberry Pi (Raspberry Pi OS)

### Paso 0 · Preparar la tarjeta microSD (Raspberry Pi Imager)

Necesitas una microSD de 8 GB o más (mejor de clase A1) y un ordenador con lector de tarjetas.

1. Descarga e instala **Raspberry Pi Imager** desde https://www.raspberrypi.com/software/ (hay versión para Windows, macOS y Linux).
2. Mete la microSD en el ordenador, abre Raspberry Pi Imager y elige:
   - **Dispositivo:** Raspberry Pi 3 (la 3A+ está en ese grupo).
   - **Sistema operativo:** *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite**. La Lite no tiene escritorio, que no hace falta.
     - Con los 512 MB de la 3A+ va algo más holgada la de 32 bits.
     - La de 64 bits también vale: el instalador funciona con las dos.
   - **Almacenamiento:** tu microSD. Se borrará entera.
3. Cuando te ofrezca **personalizar el sistema**, rellénalo. En versiones antiguas se llama «Editar ajustes» y sale al pulsar Siguiente. Es lo que permite usar la Pi sin pantalla ni teclado:
   - **Nombre (hostname):** por ejemplo `robotrc`. Luego entrarás con `robotrc.local`.
   - **Usuario y contraseña:** los tuyos, por ejemplo usuario `robot`. Apúntalos.
   - **WiFi:** el nombre de tu red, su contraseña y el país **ES**.
   - **Zona horaria y teclado:** Europe/Madrid y `es`.
   - **Servicios:** activa **SSH** con contraseña.
4. Pulsa **Escribir** y espera a que termine y verifique. Saca la tarjeta.
5. Métela en la Pi y enciéndela. El primer arranque tarda **2–3 minutos**: se reinicia sola y se conecta a tu WiFi.
6. Comprueba que responde desde el ordenador:
   ```bash
   ping robotrc.local
   ```
   Si no responde por el nombre, busca su IP en la lista de dispositivos de tu router.

### Instalación automática (recomendada)

Con la tarjeta del paso 0 la Pi ya tiene **Raspberry Pi OS** (vale Bookworm o Bullseye, también la Lite), está conectada a tu WiFi y tiene SSH activado.

1. Entra en la Pi desde tu ordenador. En Windows funciona igual en PowerShell (Windows 10/11 ya traen `ssh`). Si el nombre no funciona, usa la IP de la Pi:
   ```bash
   ssh TU_USUARIO@robotrc.local
   ```
2. Descarga el instalador y ejecútalo:
   ```bash
   curl -fsSLO https://raw.githubusercontent.com/Natxo1000/RCWebCar/main/instalar.sh
   sudo bash instalar.sh
   ```
   El programa lo descarga él solo de GitHub. Si la Pi no tiene Internet, copia con `scp` el `instalar.sh` y el `robot_rc_software.zip` (o la carpeta del repositorio) a la Pi: si encuentra el programa a su lado, usa ese.
3. Contesta a las preguntas. En las que tienen un valor entre corchetes, basta con pulsar Intro:
   - **Nombre del robot en la red:** luego entras con `http://NOMBRE.local:8080`.
   - **IP fija:** opcional. Hace falta si vas a abrir puertos en el router.
   - **INA219:** solo si lo has montado.
   - **Acceso desde Internet con login:** opcional. Ver el apartado 6.
   - **Tailscale:** opcional.
4. Al terminar reinicia la Pi, que lo pregunta ella misma. Después abre `http://NOMBRE.local:8080`: suenan 2 pitidos al arrancar y 1 al conectarte.

**Qué hace el instalador:**
- **Librerías:** instala `python3-aiohttp`, `python3-gpiozero` y `lgpio` (y `smbus2` si tienes el INA219).
- **`config.txt`:** activa el PWM por hardware de los servos, quita el audio (comparte el PWM) y activa la detección de la cámara.
- **Programa:** lo instala en `~/robot_rc`.
- **MediaMTX:** descarga la última versión para tu Pi en `~/mediamtx` y la configura: cámara a 1296×972 y 30 fps, vídeo WebRTC por TCP 8189 y solo accesible a través del servidor del robot.
- **Servicios:** crea `robot_rc` y `mediamtx`, que arrancan solos al encender.
- **Red:** pone el nombre en la red, la IP fija, el usuario admin y el dominio en `acceso.json`, añade tu dominio a `webrtcAdditionalHosts` y, si eliges el TURN de Cloudflare, activa la API de MediaMTX.

**Para actualizar el software,** vuelve a descargar el instalador y ejecútalo (baja la última versión de GitHub):
- guarda una copia de la instalación anterior (`~/robot_rc.copia-FECHA`);
- conserva tus usuarios (`acceso.json`);
- si se lo dices, conserva tus ajustes de calibración de `server.py`.

> Si al ejecutarlo sale un error con `$'\r'`, el archivo se ha guardado con saltos de línea de Windows. Arréglalo con `sed -i 's/\r$//' instalar.sh` y vuelve a ejecutarlo.

### Instalación a mano (si prefieres hacerlo tú)

1. Librerías: `sudo apt install python3-aiohttp python3-gpiozero` (y `python3-smbus2` si usas el INA219).
2. Al final de `/boot/firmware/config.txt` (en sistemas antiguos, `/boot/config.txt`):
   ```
   dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
   dtparam=audio=off
   ```
3. Copia la carpeta `robot_rc` a `~/robot_rc`.
4. Instala [MediaMTX](https://github.com/bluenviron/mediamtx). En `mediamtx.yml` pon `webrtcLocalUDPAddress: ''`, `webrtcLocalTCPAddress: ':8189'` y un *path* `cam` con `source: rpiCamera`. La web pide el vídeo al propio servidor (`/cam/whep`), que se lo pasa a MediaMTX en `127.0.0.1:8889`.
5. En `robot_rc.service` cambia `TU_USUARIO` por tu usuario y actívalo:
   ```bash
   sudo cp robot_rc.service /etc/systemd/system/
   sudo systemctl enable --now robot_rc
   ```
   Tu usuario tiene que estar en los grupos `gpio` y `video`.
6. Reinicia y abre `http://<ip-de-la-pi>:8080`.

Para ver los registros si algo falla:
```bash
journalctl -u robot_rc -n 50 --no-pager
journalctl -u mediamtx -n 50 --no-pager
```

## 3. Controles

| | PC | Móvil |
|---|---|---|
| Conducir | W A S D | Estilo emisora: el mando izquierdo acelera y frena (arriba/abajo); el derecho gira (izquierda/derecha) |
| Cámara | Flechas · C la centra | Arrastrar el dedo sobre el vídeo · doble toque o botón "Centrar cámara" |
| Velocidad | Q / E | Barra de arriba |
| Parar | Espacio | Botón "Parar" |
| Faros | L | Botón "Faros" |
| Pito | H (mantener) | Botón "Pito" (mantener) |

- **Seguridad:** si dejan de llegar órdenes durante 0,5 s, por ejemplo porque se corta la WiFi, los motores se paran solos.
- **Varias pantallas a la vez:** conduce la que mueve el coche; las demás no hacen nada hasta que esa lleve 1 s quieta (`MANDO_S`).
- **Visión nocturna:** cuando la cámara pasa a modo noche, la imagen sale con un tono morado o azulado. Es normal: la cámara quita su filtro de infrarrojos para ver con sus focos. En el botón ⚙ de la barra puedes elegir cómo verla: **Color** (tal cual), **B/N auto** (pasa a blanco y negro sola en modo noche; con los faros encendidos no cambia) o **B/N siempre**. Cada móvil u ordenador recuerda su elección.

## 4. Ajustes (al principio de `server.py`)

| Ajuste | Para qué |
|---|---|
| `INVERT_IZQ`, `INVERT_DER`, `INTERCAMBIAR_LADOS` | Si un lado gira al revés o el coche gira al contrario de lo que mandas |
| `COMPENSA_IZQ`, `COMPENSA_DER` | Para que vaya recto: baja un poco el del lado que corre más |
| `PAN_TRIM`, `TILT_TRIM` | Centro de la cámara, en grados |
| `PAN_MIN/MAX`, `TILT_MIN/MAX` | Recorrido de la cámara. Bájalo si la cabeza toca o el cable de la cámara se tensa |
| `GIRO_SITIO`, `GIRO_MARCHA`, `RAMPA_*` | Suavidad del giro y de la aceleración |
| `PITO_ARRANQUE`, `PITO_CONECTAR` | Los pitidos al arrancar y al conectar |
| `BAT_SENSOR` | `True` si montas el INA219: la web mostrará el % de batería |
| `VEL_GUEST` | Velocidad máxima de los usuarios guest (acceso desde Internet) |
| `LOGIN_MAX_FALLOS`, `LOGIN_VENTANA_S` | Intentos de login fallidos permitidos y en cuánto tiempo |

Después de cambiar un ajuste, reinicia el servicio:
```bash
sudo systemctl restart robot_rc
```

## 5. Importante

En tu red local (o por una VPN como Tailscale) la web **no pide contraseña**: quien esté en tu red puede conducir el coche. **No abras los puertos 8080 ni 8889 a Internet.** Para entrar desde Internet usa el apartado 6, que sí pide usuario y contraseña.

## 6. Acceso desde Internet con login (opcional)

Sirve para que alguien conduzca desde fuera con usuario y contraseña, sin instalar nada. Necesitas un dominio con HTTPS y un **proxy inverso** (nginx, Caddy, el de un Synology...) que lleve ese dominio al puerto 8080 de la Pi, **con WebSocket activado**.

El vídeo no pasa por el proxy, porque es WebRTC. Elige una de estas dos formas:
- **A. Abrir un puerto para el vídeo (lo más sencillo).**
  - Reenvía en el router el puerto **8189** a la Pi. Puede ser UDP o TCP: el que tengas activo en `webrtcLocalUDPAddress` o `webrtcLocalTCPAddress` de `mediamtx.yml`.
  - Añade tu dominio a `webrtcAdditionalHosts` en `mediamtx.yml`.
  - Ese puerto solo lleva el vídeo de conexiones que ya han hecho login.
- **B. Sin abrir puertos, con el TURN de Cloudflare.**
  - Crea en Cloudflare un **TURN Server** (Realtime → TURN Server). Pide tarjeta, pero incluye 1.000 GB al mes.
  - Anota el *Turn Token ID* y el *API Token*.
  - En `mediamtx.yml` pon `api: yes` y `apiAddress: 127.0.0.1:9997`.

Si usaste `instalar.sh` y contestaste que sí al acceso desde Internet, los pasos 1 y 2 ya están hechos; solo te queda el proxy inverso y, con la opción A, abrir el puerto en el router.

Pasos (a mano):
1. En la carpeta del servidor, crea tu usuario de admin y edita el archivo que se crea:
   ```bash
   python3 server.py clave yo admin TuContraseña
   nano acceso.json
   ```
   - Si no pones contraseña, se inventa una.
   - Rellena `dominio` y `proxy_ips` (la IP local del proxy inverso).
   - Solo con la opción B, rellena también `cloudflare_turn` (`key_id` y `api_token`).
   - No hace falta reiniciar: el servidor vuelve a leer el archivo solo.
2. Con la opción B, en `journalctl -u robot_rc` deben salir `TURN: credenciales nuevas de Cloudflare` y `TURN: MediaMTX actualizado`.
3. Entra en `https://tu-dominio/`: sale la pantalla de login.
4. Crea los invitados desde la web (botón 👥) o desde la Pi:
   ```bash
   python3 server.py clave amigo
   python3 server.py claves
   python3 server.py quitar amigo
   ```

Cómo funciona:
- **El login:**
  - Lo que llega por el proxy pide usuario y contraseña.
  - La sesión queda guardada en el navegador, en una cookie firmada que no lleva la contraseña.
  - Para salir: ⚙ → **Cerrar sesión**.
  - Tras 10 intentos fallidos en 10 minutos (`LOGIN_MAX_FALLOS`), esa conexión tiene que esperar.
- **Tipos de usuario:** **admin** (todo) y **guest** (velocidad limitada con `VEL_GUEST`).
- **Botón 👥 en la barra**, para los admin y para quien entra desde la red local:
  - quién está conectado y quién conduce;
  - **Echar** a un guest, que puede volver a entrar, o **Quitar acceso**, que además borra su usuario;
  - crear invitados: da la dirección, el usuario y la contraseña, con Copiar y Compartir; y borrarlos.
- **Si hay un guest conectado,** la pantalla del admin pasa a **"Solo ver"** para no estorbarle:
  - **Tomar el control** (o Parar): conduce el admin y el guest solo mira;
  - **Devolver el control:** vuelve a conducir el guest.
- **En la red local o por VPN** todo sigue igual: sin login y con el vídeo directo.
- **`acceso.json`** lleva las contraseñas: no lo compartas.
