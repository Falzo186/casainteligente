"""
config.py — Configuración central de "Casa Inteligente en Caja" (lado PC).

Todos los módulos (stt, tts, entendedor, serial_client, dialogo, main) leen sus parámetros desde
aquí; ninguno debe tener "números mágicos" propios.

Los valores que cambian de un equipo a otro (puerto USB de la placa, modelo de Ollama,
micrófono, etc.) se pueden sobrescribir con variables de entorno, sin tocar
el código:

    Windows (PowerShell):  $env:CASA_PUERTO = "COM5"
    Linux / macOS:         export CASA_PUERTO=/dev/ttyUSB0

Para ver la configuración efectiva y validarla:

    python config.py
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Utilidades para leer variables de entorno
# ---------------------------------------------------------------------------

def _a_bool(texto: str) -> bool:
    """Convierte textos como 'true', '1', 'sí', 'on' en booleano."""
    valor = texto.strip().lower()
    if valor in ("1", "true", "si", "sí", "yes", "on"):
        return True
    if valor in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"no es un booleano: {texto!r}")


def _int_opcional(texto: str) -> int | None:
    """Convierte a entero; 'none' o '' significan 'sin valor' (None)."""
    valor = texto.strip().lower()
    if valor in ("", "none", "null"):
        return None
    return int(valor)


def _env(nombre: str, por_defecto: T, conversor: Callable[[str], T] | None = None) -> T:
    """
    Lee la variable de entorno `nombre` y la convierte al tipo adecuado.

    Si no existe, está vacía o no se puede convertir, devuelve `por_defecto`
    (avisando por consola en el último caso, para que el error no pase
    desapercibido).
    """
    crudo = os.environ.get(nombre)
    if crudo is None or crudo.strip() == "":
        return por_defecto

    if conversor is None:
        conversor = _a_bool if isinstance(por_defecto, bool) else type(por_defecto)

    try:
        return conversor(crudo.strip())
    except (TypeError, ValueError):
        print(f"[config] Aviso: {nombre}={crudo!r} no es válido; se usa {por_defecto!r}")
        return por_defecto


# ---------------------------------------------------------------------------
# Comunicación con la placa por USB / puerto serie (firmware/casa_serial)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigSerie:
    # "auto" busca la placa (chips CP210x, CH340, FTDI o USB nativo del ESP32);
    # o un puerto concreto: "COM5" en Windows, "/dev/ttyUSB0" en Linux.
    puerto: str = _env("CASA_PUERTO", "auto")
    baudios: int = 115200
    timeout_lectura_s: float = 0.1          # tiempo máximo de cada lectura del puerto
    timeout_respuesta_s: float = 1.5        # espera a la respuesta de cada orden
    # Al abrir el puerto, muchas placas ESP32 se reinician; se espera este
    # tiempo a que arranquen (responde a PING) antes de dar la conexión por mala.
    espera_arranque_s: float = 4.0
    intervalo_estado_s: float = _env("CASA_SONDEO_S", 1.0)   # cada cuánto se pide STATUS


# ---------------------------------------------------------------------------
# Reconocimiento de voz (Faster-Whisper + captura con sounddevice)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigSTT:
    # --- Modelo Whisper ---
    modelo: str = _env("CASA_WHISPER_MODELO", "base")
    idioma: str = "es"
    dispositivo: str = _env("CASA_WHISPER_DISPOSITIVO", "cpu")    # "cpu" o "cuda"
    tipo_computo: str = _env("CASA_WHISPER_COMPUTO", "int8")      # int8 en CPU, float16 en GPU
    beam_size: int = 1            # 1 = búsqueda voraz (más rápido); 5 = algo más preciso
    vad_filter: bool = True       # VAD interno (Silero) de faster-whisper: recorta silencios
    # Pista de vocabulario para Whisper: mejora el reconocimiento de estas palabras.
    prompt_inicial: str = (
        "Comandos de casa: enciende, apaga, prende, luces, ventilador, sala, "
        "patio trasero, fachada, modo seguridad."
    )

    # --- Captura de audio ---
    frecuencia_muestreo: int = 16000   # Whisper espera 16 kHz mono
    canales: int = 1
    duracion_bloque_ms: int = 30       # tamaño de cada bloque leído del micrófono
    # Índice del micrófono (None = el predeterminado del sistema).
    # Lista los disponibles con:  python -m sounddevice
    dispositivo_entrada: int | None = _env("CASA_MIC_INDICE", None, _int_opcional)

    # --- Detección de voz en tiempo real (por energía RMS) ---
    # El umbral real es max(umbral_energia_rms, ruido_ambiente * factor_sobre_ruido).
    umbral_energia_rms: float = _env("CASA_UMBRAL_RMS", 0.015)   # escala 0..1
    calibrar_ruido_al_iniciar: bool = True
    duracion_calibracion_s: float = 1.0
    factor_sobre_ruido: float = 3.0

    bloques_inicio_voz: int = 3          # bloques seguidos con voz para empezar a grabar (~90 ms)
    factor_histeresis: float = 0.6       # ya grabando, el umbral baja a umbral*factor
                                         # (para no cortar finales de palabra suaves)
    silencio_fin_frase_s: float = 0.8    # silencio que da por terminada una frase
    duracion_min_frase_s: float = 0.25   # tiempo mínimo CON voz; descarta golpes y clics
    duracion_max_frase_s: float = 10.0   # corta frases demasiado largas
    preroll_s: float = 0.3               # audio previo al disparo, para no cortar la 1.ª sílaba

    # Tiempo máximo esperando a que el usuario empiece a hablar cuando se le
    # hizo una pregunta de aclaración. En escucha normal no hay timeout.
    timeout_escucha_s: float = 8.0
    # Si el micrófono deja de enviar audio durante este tiempo, se considera
    # desconectado (cable suelto, otro programa lo bloqueó, etc.).
    timeout_sin_audio_s: float = 2.0

    # --- Filtro de "alucinaciones" de Whisper ---
    # Con ruido o silencio, Whisper a veces inventa frases. Se descartan los
    # segmentos con alta probabilidad de no-voz y baja confianza.
    max_prob_no_voz: float = 0.6
    min_logprob_medio: float = -1.0


# ---------------------------------------------------------------------------
# Síntesis de voz (pyttsx3, offline)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigTTS:
    velocidad: int = _env("CASA_TTS_VELOCIDAD", 165)   # palabras por minuto aprox.
    volumen: float = 1.0                                # 0.0 .. 1.0
    # Se elige la primera voz instalada cuyo id/nombre/idioma contenga alguna de
    # estas PALABRAS completas (en Windows: "Microsoft Helena/Sabina/Laura",
    # cuyo id contiene "ES-ES" o "ES-MX"; en Linux, la voz "spanish" de eSpeak).
    palabras_clave_voz: tuple[str, ...] = (
        "es", "spanish", "español", "helena", "sabina", "laura", "pablo", "raul",
    )
    # Algunas instalaciones de pyttsx3 solo hablan la primera vez que se llama
    # a runAndWait(). Si te pasa, pon esto en True (crea un motor por frase,
    # cuesta ~0.2 s más por respuesta).
    reiniciar_motor_cada_frase: bool = _env("CASA_TTS_REINICIAR", False)


# ---------------------------------------------------------------------------
# Modelo de lenguaje (Ollama)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigLLM:
    host: str = _env("CASA_OLLAMA_HOST", "http://localhost:11434")
    modelo: str = _env("CASA_OLLAMA_MODELO", "qwen2.5:1.5b")
    temperatura: float = 0.1     # baja: queremos respuestas deterministas
    top_p: float = 0.9
    max_tokens: int = 150        # num_predict: el JSON esperado es corto
    timeout_s: float = 30.0
    keep_alive: str = "10m"      # mantiene el modelo cargado entre comandos


# ---------------------------------------------------------------------------
# La casa (firmware/casa_serial): zonas y música
# ---------------------------------------------------------------------------

# clave -> (número de foco en la placa, nombre con artículo, nombre sin artículo).
# El orden es el de los focos 1 a 6 del firmware.
ZONAS_CASA: dict[str, tuple[int, str, str]] = {
    "bano": (1, "el baño", "baño"),
    "cocina": (2, "la cocina", "cocina"),
    "comedor": (3, "el comedor", "comedor"),
    "sala": (4, "la sala", "sala"),
    "cuarto": (5, "el cuarto", "cuarto"),
    "exterior": (6, "el exterior", "exterior"),
}

VOLUMEN_MAX = 30      # el DFPlayer admite 0 a 30
VOLUMEN_PASO = 4      # lo que suben/bajan "sube el volumen" / "baja el volumen"
PISTA_ALARMA = 1      # 0001.mp3 en la tarjeta SD: el sonido de la alarma
PISTA_MIN_CANCION = 2 # las canciones van de 0002.mp3 en adelante

# Lista de canciones (título, alias y número de archivo): se edita en pc/canciones.json
ARCHIVO_CANCIONES = "canciones.json"


# ---------------------------------------------------------------------------
# Diálogo y aclaraciones
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigDialogo:
    # Tiempo de vida del contexto de aclaración pendiente: pasado este tiempo
    # se descarta y la siguiente frase se trata como un comando nuevo.
    timeout_aclaracion_s: float = 15.0
    max_intentos_aclaracion: int = 2
    frases_cancelar: tuple[str, ...] = (
        "cancela", "cancelar", "olvídalo", "olvidalo", "déjalo", "dejalo", "nada",
    )
    # Cada cuánto se revisan las alertas de seguridad mientras se escucha
    # (también durante una aclaración, para no retrasar un aviso de alarma).
    intervalo_alertas_s: float = 0.5
    # Pausa entre intentos de recuperar el micrófono si se desconecta.
    pausa_reintento_microfono_s: float = 3.0
    mensaje_no_entendido: str = "No entendí el comando. ¿Puedes repetirlo?"
    mensaje_sin_aclaracion: str = "No te entendí, así que cancelo el comando."
    mensaje_sin_respuesta: str = "No te escuché, así que cancelo el comando."
    mensaje_error_red: str = "No pude comunicarme con la placa. Revisa el cable USB."
    mensaje_error_llm: str = "No puedo consultar el modelo de lenguaje. ¿Está Ollama en marcha?"
    mensaje_cancelado: str = "De acuerdo, lo cancelo."
    mensaje_inicio: str = "Casa inteligente lista."
    mensaje_sin_microfono: str = "Perdí el micrófono. Intentaré reconectarlo."
    mensaje_conexion_perdida: str = (
        "Atención: perdí la conexión con la casa y el modo seguridad está activo."
    )
    mensaje_alerta_intruso: str = "¡Alerta de seguridad! Se detectó movimiento."


# ---------------------------------------------------------------------------
# Registro (logging)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigLog:
    nivel: str = _env("CASA_LOG", "INFO")   # DEBUG, INFO, WARNING, ERROR
    formato: str = "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s: %(message)s"
    formato_fecha: str = "%H:%M:%S"


# ---------------------------------------------------------------------------
# Instancias que importan los demás módulos:  from config import SERIE, STT, ...
# ---------------------------------------------------------------------------

SERIE = ConfigSerie()
STT = ConfigSTT()
TTS = ConfigTTS()
LLM = ConfigLLM()
DIALOGO = ConfigDialogo()
LOG = ConfigLog()


def validar() -> list[str]:
    """
    Comprueba la coherencia de la configuración.

    Devuelve una lista de problemas en texto (vacía si todo está bien).
    main.py la llama al arrancar para fallar pronto con un mensaje claro.
    """
    problemas: list[str] = []

    # --- Puerto serie ---
    if not SERIE.puerto.strip():
        problemas.append("SERIE.puerto no puede estar vacío (usa 'auto' o un nombre como COM5).")
    if SERIE.baudios <= 0:
        problemas.append("SERIE.baudios debe ser positivo.")
    if min(SERIE.timeout_lectura_s, SERIE.timeout_respuesta_s, SERIE.espera_arranque_s) <= 0:
        problemas.append("Los timeouts de SERIE deben ser mayores que 0.")
    if SERIE.intervalo_estado_s < 0.2:
        problemas.append("SERIE.intervalo_estado_s < 0.2 s saturaría la placa.")

    # --- STT ---
    if STT.frecuencia_muestreo != 16000:
        problemas.append("STT.frecuencia_muestreo debe ser 16000 (lo que espera Whisper).")
    if STT.canales != 1:
        problemas.append("STT.canales debe ser 1 (mono).")
    if STT.dispositivo not in ("cpu", "cuda", "auto"):
        problemas.append(f"STT.dispositivo desconocido: {STT.dispositivo!r}")
    if not 0 < STT.umbral_energia_rms < 1:
        problemas.append("STT.umbral_energia_rms debe estar entre 0 y 1.")
    if not STT.duracion_min_frase_s < STT.duracion_max_frase_s:
        problemas.append("STT.duracion_min_frase_s debe ser menor que duracion_max_frase_s.")
    if STT.silencio_fin_frase_s >= STT.duracion_max_frase_s:
        problemas.append("STT.silencio_fin_frase_s debe ser menor que duracion_max_frase_s.")
    if STT.bloques_inicio_voz < 1:
        problemas.append("STT.bloques_inicio_voz debe ser al menos 1.")
    if not 0 < STT.factor_histeresis <= 1:
        problemas.append("STT.factor_histeresis debe estar entre 0 (excluido) y 1.")
    if not 10 <= STT.duracion_bloque_ms <= 100:
        problemas.append("STT.duracion_bloque_ms debe estar entre 10 y 100 ms.")

    # --- TTS ---
    if not 0.0 <= TTS.volumen <= 1.0:
        problemas.append("TTS.volumen debe estar entre 0.0 y 1.0.")

    # --- LLM ---
    if not 0.0 <= LLM.temperatura <= 1.0:
        problemas.append("LLM.temperatura debe estar entre 0.0 y 1.0.")
    if not LLM.host.startswith(("http://", "https://")):
        problemas.append(f"LLM.host debe empezar por http:// o https://: {LLM.host!r}")

    # --- Diálogo ---
    if DIALOGO.timeout_aclaracion_s <= STT.timeout_escucha_s:
        problemas.append(
            "DIALOGO.timeout_aclaracion_s debe ser mayor que STT.timeout_escucha_s "
            "(si no, la aclaración caduca antes de que el usuario termine de responder)."
        )
    if DIALOGO.max_intentos_aclaracion < 1:
        problemas.append("DIALOGO.max_intentos_aclaracion debe ser al menos 1.")
    if DIALOGO.intervalo_alertas_s <= 0:
        problemas.append("DIALOGO.intervalo_alertas_s debe ser mayor que 0.")

    return problemas


if __name__ == "__main__":
    # Muestra la configuración efectiva y el resultado de la validación.
    for nombre, seccion in (("SERIE", SERIE), ("STT", STT), ("TTS", TTS),
                            ("LLM", LLM), ("DIALOGO", DIALOGO), ("LOG", LOG)):
        print(f"[{nombre}]")
        for clave, valor in asdict(seccion).items():
            print(f"  {clave:28} = {valor!r}")
    print()

    errores = validar()
    if errores:
        print("Configuración con problemas:")
        for e in errores:
            print(f"  - {e}")
        raise SystemExit(1)
    print("Configuración OK.")
