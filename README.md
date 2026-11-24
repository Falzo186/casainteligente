# Casa Inteligente en Caja

Una casa domótica en miniatura controlada con **voz o texto** desde una PC, con una
interfaz gráfica y un asistente de IA local (todo funciona sin internet).

```
 micrófono ─► Whisper ─► reglas ──► placa ESP32 (USB) ─► focos, ventilador, alarma, música
                           └─(si no entiende)─► LLM pequeño (Ollama) + confirmación
 pantalla  ◄─ interfaz PySide6 ◄─ sensores, estado, eventos del guardián
 altavoz   ◄─ pyttsx3 (respuestas habladas)
```

## Qué puede hacer

| Dices | Pasa |
|---|---|
| «prende la luz de la sala» / «apaga el baño» | enciende o apaga el foco (6 zonas) |
| «prende la sala y el cuarto» · «apaga todas las luces» | varias zonas a la vez |
| «prende las luces» | pregunta «¿Qué luces quieres encender…?» y fusiona tu respuesta |
| «enciende el ventilador» · «apaga todo» | ventilador, o todo junto |
| «activa el modo guardián» | arma el PIR; si detecta movimiento suena la alarma y la PC avisa |
| «apaga la alarma» | silencia la sirena y desarma el guardián |
| «pon Thriller» · «pon la canción tres» · «pon cualquier canción» | reproduce desde la SD |
| «para la música» · «sube el volumen» · «volumen al 50» | controla el reproductor |
| «qué temperatura hace» · «cómo está la casa» | lee los sensores |

Zonas: **baño, cocina, comedor, sala, cuarto, exterior** (patio, jardín y entrada cuentan como exterior).

### Cómo entiende (y por qué es rápido y seguro)

1. **Reglas primero** ([nlu.py](pc/nlu.py)): resuelven casi todo en ~0,3 ms y nunca inventan datos.
2. **Modelo de respaldo** ([entendedor.py](pc/entendedor.py)): solo si las reglas no reconocen nada.
   Un modelo de 1,5B es rápido (~0,4 s) pero poco fiable, así que su salida se restringe con un
   esquema JSON, se descartan las combinaciones absurdas y **todo lo que supone se confirma**
   («¿Quieres que desactive el modo guardián?»). Sin Ollama, las reglas siguen funcionando.

## Estructura

```
casainteligente/
├── pc/
│   ├── gui_app.py          interfaz gráfica (PySide6)          ← lo normal es usar esto
│   ├── gui_widgets.py      componentes visuales (plano, medidores, chat, música…)
│   ├── main.py             el mismo asistente en la consola
│   ├── dialogo.py          conversación, aclaraciones, alertas y fuentes de entrada
│   ├── nlu.py              comprensión por reglas
│   ├── entendedor.py       reglas + LLM de respaldo con confirmación
│   ├── acciones.py         traduce un comando a órdenes de la placa y a una frase hablada
│   ├── serial_client.py    protocolo USB con la placa + monitor con reconexión
│   ├── simulador_casa.py   la placa simulada (tests y modo --demo)
│   ├── stt.py              micrófono + Faster-Whisper (español, detección de voz)
│   ├── tts.py              pyttsx3 (voz en español, sin internet)
│   ├── catalogo.py         lista de canciones
│   ├── canciones.json      ← EDITA ESTO con tus canciones
│   ├── config.py           todos los parámetros (se pueden sobrescribir con variables CASA_*)
│   └── requirements.txt
├── firmware/
│   ├── casa_serial/        ← firmware que se usa (USB)
│   └── casa_inteligente/   versión Wi-Fi/HTTP anterior (ya no la usa la PC; se conserva por si acaso)
└── tests/                  pruebas automáticas
```

## Instalación

### 1. La PC (Python 3.11 o superior)

```bash
cd pc
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

La primera vez que actives el micrófono se descarga el modelo Whisper `base` (~150 MB).

### 2. El modelo de lenguaje (opcional pero recomendado)

Instala [Ollama](https://ollama.com) y descarga el modelo pequeño:

```bash
ollama pull qwen2.5:1.5b
```

Para usar otro (por ejemplo `llama3.2:1b`): `$env:CASA_OLLAMA_MODELO = "llama3.2:1b"`.

### 3. Una voz en español (Windows)

Configuración → Hora e idioma → Voz → Agregar voces → Español. Comprueba cuál se elige con
`python tts.py`.

### 4. La placa (Arduino IDE)

1. Placa: **ESP32 Dev Module** (paquete «esp32» de Espressif).
2. Librerías (Gestor de librerías): **DFRobotDFPlayerMini** y **DHT sensor library**
   (acepta instalar *Adafruit Unified Sensor*).
3. Abre [firmware/casa_serial/casa_serial.ino](firmware/casa_serial/casa_serial.ino) y súbelo.
4. **Cierra el Monitor Serie** antes de abrir la interfaz: solo un programa puede usar el puerto.

### 5. La tarjeta SD del DFPlayer

Formato **FAT32**, tarjeta vacía. Copia los archivos **en orden**, uno por uno:

| Archivo | Contenido |
|---|---|
| `0001.mp3` | el sonido de la alarma |
| `0002.mp3` … `0011.mp3` | tus canciones |

Después edita [pc/canciones.json](pc/canciones.json): `pista` es el número del archivo,
`titulo` lo que se dice en voz alta y `alias` otras formas de pedirla («la de Queen»).

## Cableado (resumen)

| Componente | Pin ESP32 |
|---|---|
| Focos 1 Baño · 2 Cocina · 3 Comedor · 4 Sala · 5 Cuarto · 6 Exterior | 13 · 14 · 27 · 26 · 25 · 33 |
| Relé del ventilador (activo en bajo) | 23 |
| Sensor PIR | 18 |
| DHT11 (pin S) | 19 |
| DFPlayer RX ← / TX → | 17 / 16 (cruzados: TX2→RX, RX2←TX) |

Los pines y la polaridad se cambian al principio de `casa_serial.ino`.

## Uso

Desde la carpeta `pc/`:

```bash
python gui_app.py            # interfaz con la placa por USB
python gui_app.py --demo     # interfaz con la placa simulada (sin hardware)
python main.py               # consola con micrófono
python main.py --texto       # consola escribiendo
python main.py --demo --texto
```

En la interfaz: toca una habitación para encender o apagar su luz, escribe en el chat o
pulsa el **orbe del micrófono** (la primera vez carga Whisper). El botón de arriba a la
derecha arma el guardián; con alarma, apaga la sirena.

Si el puerto no se detecta solo: `$env:CASA_PUERTO = "COM5"` (lista de puertos en el
Administrador de dispositivos).

## Probarlo (lista de verificación)

**Sin hardware** — todo lo que sigue se puede comprobar con `--demo`:

```bash
cd ..
python -m unittest discover -s tests -v
```

**Con la placa**, en este orden:

1. Monitor Serie (115200, «Nueva línea»): `PING` → `PONG`; `STATUS` → una línea con los sensores; `LED 4 1` → enciende la sala.
2. `python serial_client.py estado` → muestra el estado; `python serial_client.py led 4 on`.
3. `python tts.py` → oyes la voz en español.
4. `python stt.py` → lo que dices aparece escrito (prueba `python stt.py --microfonos` para elegir micrófono con `CASA_MIC_INDICE`).
5. `python main.py --texto` → «prende la sala», «pon música», «activa el modo guardián» (camina frente al PIR).
6. `python gui_app.py` → lo mismo con la interfaz; prueba a quitar y volver a poner el cable USB.

## Variables de entorno útiles

| Variable | Para qué |
|---|---|
| `CASA_PUERTO` | puerto serie de la placa (`auto` por defecto) |
| `CASA_OLLAMA_MODELO` | modelo de respaldo (`qwen2.5:1.5b`) |
| `CASA_WHISPER_MODELO` | `base` por defecto; `small` es más preciso y más lento |
| `CASA_MIC_INDICE` | número del micrófono (`python stt.py --microfonos`) |
| `CASA_UMBRAL_RMS` | sensibilidad de la detección de voz |
| `CASA_TTS_REINICIAR` | `1` si la voz solo suena la primera vez |
| `CASA_LOG` | `DEBUG` para ver todo |

Lista completa y valores por defecto: `python config.py`.

## Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| «No se pudo abrir COM5… ¿Está abierto el Monitor Serie?» | cierra el Monitor Serie del Arduino IDE |
| «La placa no responde» | no está cargado `casa_serial.ino`, o es otro puerto |
| «El reproductor no está conectado» | revisa el cableado del DFPlayer y la SD (FAT32) |
| Suena otra canción de la esperada | copia los archivos a la SD en orden, uno por uno |
| La PC se «oye» a sí misma | baja el volumen del altavoz o usa auriculares |
| Solo habla la primera vez | `CASA_TTS_REINICIAR=1` |
| «Ollama no está disponible» | no es grave: funcionan las órdenes directas |
