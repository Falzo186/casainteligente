"""
stt.py — Reconocimiento de voz offline con Faster-Whisper.

Flujo:
    micrófono (sounddevice, bloques de 30 ms)
      -> SegmentadorVoz: detecta inicio/fin de frase por energía (RMS)
      -> Whisper "base" en español (con su VAD Silero para recortar silencios)
      -> filtro de "alucinaciones" -> texto

El micrófono queda abierto todo el tiempo y el audio se acumula en una cola.
Por eso, después de que la PC hable por el altavoz, main.py debe llamar a
descartar_audio() para no transcribir su propia voz.

Prueba manual:

    python stt.py               # escucha y muestra lo que entiende
    python stt.py --microfonos  # lista los micrófonos disponibles
"""

from __future__ import annotations

import logging
import queue
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass

import numpy as np

from config import STT, ConfigSTT

try:
    import sounddevice as sd
except (ImportError, OSError):  # OSError: falta la librería PortAudio del sistema
    sd = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

log = logging.getLogger("stt")


# ---------------------------------------------------------------------------
# Excepciones y resultados
# ---------------------------------------------------------------------------

class ErrorMicrofono(Exception):
    """No hay micrófono, no se pudo abrir o dejó de enviar audio."""


class ErrorSTT(Exception):
    """Falló la carga del modelo Whisper o la transcripción."""


@dataclass(frozen=True)
class Transcripcion:
    texto: str
    duracion_audio_s: float        # duración de la frase grabada
    tiempo_transcripcion_s: float  # lo que tardó Whisper (para el registro de tiempos)


# ---------------------------------------------------------------------------
# Utilidades de audio y texto (funciones puras, probadas en tests/test_stt.py)
# ---------------------------------------------------------------------------

def energia_rms(bloque: np.ndarray) -> float:
    """Energía RMS de un bloque de audio float32 en el rango -1..1."""
    if bloque.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(bloque, dtype=np.float64))))


def remuestrear(audio: np.ndarray, origen: int, destino: int) -> np.ndarray:
    """
    Cambia la frecuencia de muestreo con interpolación lineal.

    Solo se usa si el micrófono no admite 16 kHz. No aplica filtro
    antialiasing, pero para reconocer voz la diferencia es despreciable.
    """
    if origen == destino or audio.size == 0:
        return audio
    n = int(round(audio.size * destino / origen))
    x_origen = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_destino = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_destino, x_origen, audio).astype(np.float32)


def _plano(texto: str) -> str:
    """Minúsculas, sin tildes y con espacios simples, para comparar textos."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_tildes.split())


# Frases que Whisper suele inventar con ruido o silencio (vienen de los
# subtítulos de YouTube con los que se entrenó).
_PATRONES_ALUCINACION = [
    r"amara\s*\.?\s*org",
    r"subtitul",
    r"suscrib",
    r"gracias por ver",
    r"nos vemos en el (proximo|siguiente) video",
]


def limpiar_texto(texto: str, prompt_inicial: str = "") -> str:
    """
    Limpia la transcripción y devuelve "" si parece una alucinación.

    También descarta el caso en que Whisper repite el prompt inicial (pasa a
    veces cuando el audio es casi silencio).
    """
    texto = re.sub(r"\s+", " ", texto or "").strip()
    if not texto:
        return ""
    plano = _plano(texto)
    if any(re.search(p, plano) for p in _PATRONES_ALUCINACION):
        return ""
    if prompt_inicial:
        # Whisper repitió el prompt: empieza igual o es un trozo largo de él.
        prompt_plano = _plano(prompt_inicial)
        inicio_prompt = " ".join(prompt_plano.split()[:3])
        if plano.startswith(inicio_prompt) or (len(plano) > 20 and plano in prompt_plano):
            return ""
    # Una frase hecha solo de signos ("...", "¿?") no sirve.
    if not re.search(r"\w", texto):
        return ""
    return texto


# ---------------------------------------------------------------------------
# Segmentador: corta el audio continuo en frases
# ---------------------------------------------------------------------------

class SegmentadorVoz:
    """
    Máquina de estados que recibe bloques de audio y devuelve frases completas.

    - En espera: guarda los últimos bloques (pre-roll). Cuando hay
      `bloques_inicio_voz` bloques seguidos por encima del umbral, empieza a
      grabar, incluyendo el pre-roll para no cortar la primera sílaba.
    - Grabando: el umbral baja (histéresis). La frase termina tras
      `silencio_fin_frase_s` de silencio o al llegar a `duracion_max_frase_s`.
    - Si la frase tiene menos de `duracion_min_frase_s` con voz, se descarta
      (golpes, clics, una tos corta).
    """

    def __init__(self, umbral: float, duracion_bloque_s: float, cfg: ConfigSTT = STT) -> None:
        self.umbral = umbral
        self._cfg = cfg
        self._bloque_s = duracion_bloque_s
        bloques_preroll = max(1, round(cfg.preroll_s / duracion_bloque_s))
        self._preroll: deque[np.ndarray] = deque(maxlen=bloques_preroll + cfg.bloques_inicio_voz)
        self.reiniciar()

    def reiniciar(self) -> None:
        """Olvida cualquier frase a medias."""
        self._preroll.clear()
        self._frase: list[np.ndarray] = []
        self._votos = 0
        self._bloques_voz = 0
        self._silencio_s = 0.0

    @property
    def grabando(self) -> bool:
        return bool(self._frase)

    def procesar(self, bloque: np.ndarray) -> np.ndarray | None:
        """Añade un bloque. Devuelve el audio de la frase cuando termina; si no, None."""
        rms = energia_rms(bloque)

        if not self._frase:
            # --- En espera de voz ---
            self._preroll.append(bloque)
            self._votos = self._votos + 1 if rms >= self.umbral else 0
            if self._votos >= self._cfg.bloques_inicio_voz:
                self._frase = list(self._preroll)
                self._preroll.clear()
                self._bloques_voz = self._votos
                self._silencio_s = 0.0
            return None

        # --- Grabando ---
        self._frase.append(bloque)
        if rms >= self.umbral * self._cfg.factor_histeresis:
            self._bloques_voz += 1
            self._silencio_s = 0.0
        else:
            self._silencio_s += self._bloque_s

        duracion = len(self._frase) * self._bloque_s
        if self._silencio_s < self._cfg.silencio_fin_frase_s and duracion < self._cfg.duracion_max_frase_s:
            return None

        audio = np.concatenate(self._frase)
        voz_s = self._bloques_voz * self._bloque_s
        self._frase, self._votos, self._bloques_voz, self._silencio_s = [], 0, 0, 0.0

        if voz_s < self._cfg.duracion_min_frase_s:
            log.debug("Sonido descartado: %.2f s de voz (mínimo %.2f s)", voz_s, self._cfg.duracion_min_frase_s)
            return None
        return audio


# ---------------------------------------------------------------------------
# Reconocedor: micrófono + segmentador + Whisper
# ---------------------------------------------------------------------------

class ReconocedorVoz:
    """
    Uso típico:

        with ReconocedorVoz() as stt:
            resultado = stt.escuchar()            # bloquea hasta oír una frase
            resultado = stt.escuchar(timeout=8)   # None si nadie habla en 8 s
    """

    def __init__(self, cfg: ConfigSTT = STT) -> None:
        self._cfg = cfg
        self._bloque_muestras = int(cfg.frecuencia_muestreo * cfg.duracion_bloque_ms / 1000)
        self._bloque_s = self._bloque_muestras / cfg.frecuencia_muestreo
        self._frecuencia_captura = cfg.frecuencia_muestreo
        # Cola acotada a ~10 s: si nadie lee, se descarta el audio más antiguo.
        self._cola: queue.Queue[np.ndarray] = queue.Queue(maxsize=int(10 / self._bloque_s))
        self._stream = None
        self._modelo = None
        self._segmentador = SegmentadorVoz(cfg.umbral_energia_rms, self._bloque_s, cfg)

    def __enter__(self) -> ReconocedorVoz:
        self.iniciar()
        return self

    def __exit__(self, *exc) -> None:
        self.detener()

    @property
    def umbral(self) -> float:
        return self._segmentador.umbral

    # --- Arranque / parada --------------------------------------------------

    def iniciar(self) -> None:
        """Carga Whisper, abre el micrófono y calibra el ruido ambiente."""
        self._cargar_modelo()
        self._abrir_microfono()
        if self._cfg.calibrar_ruido_al_iniciar:
            self.calibrar()

    def detener(self) -> None:
        """Cierra el micrófono. Se puede volver a llamar a iniciar() después."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.debug("Error al cerrar el micrófono: %s", e)
            self._stream = None

    def reiniciar_microfono(self) -> None:
        """Cierra y vuelve a abrir el micrófono (tras un ErrorMicrofono)."""
        self.detener()
        self._abrir_microfono()
        self.descartar_audio()

    def _cargar_modelo(self) -> None:
        if self._modelo is not None:
            return
        if WhisperModel is None:
            raise ErrorSTT("faster-whisper no está instalado (pip install faster-whisper).")
        t0 = time.perf_counter()
        try:
            # La primera vez descarga el modelo (~150 MB para "base").
            self._modelo = WhisperModel(
                self._cfg.modelo, device=self._cfg.dispositivo, compute_type=self._cfg.tipo_computo,
            )
        except Exception as e:
            raise ErrorSTT(f"No se pudo cargar el modelo Whisper '{self._cfg.modelo}': {e}") from e
        log.info("Whisper '%s' cargado en %.1f s (%s, %s)", self._cfg.modelo,
                 time.perf_counter() - t0, self._cfg.dispositivo, self._cfg.tipo_computo)

    def _abrir_microfono(self) -> None:
        if sd is None:
            raise ErrorMicrofono("sounddevice no está instalado o falta PortAudio (pip install sounddevice).")

        dispositivo = self._cfg.dispositivo_entrada
        try:
            info = sd.query_devices(dispositivo, kind="input")
        except Exception as e:  # ValueError / PortAudioError: no existe o no hay ninguno
            raise ErrorMicrofono(
                f"No hay micrófono disponible ({e}). Conecta uno o revisa CASA_MIC_INDICE "
                "(lista: python stt.py --microfonos)."
            ) from e

        # Se intenta capturar a 16 kHz; si el micrófono no lo admite, se usa
        # su frecuencia nativa y se remuestrea por software.
        self._frecuencia_captura = self._cfg.frecuencia_muestreo
        try:
            sd.check_input_settings(device=dispositivo, channels=self._cfg.canales,
                                    samplerate=self._frecuencia_captura, dtype="float32")
        except Exception:
            self._frecuencia_captura = int(info["default_samplerate"])
            log.info("El micrófono no admite %d Hz; se captura a %d Hz y se remuestrea.",
                     self._cfg.frecuencia_muestreo, self._frecuencia_captura)

        try:
            self._stream = sd.InputStream(
                device=dispositivo,
                channels=self._cfg.canales,
                samplerate=self._frecuencia_captura,
                blocksize=int(self._frecuencia_captura * self._cfg.duracion_bloque_ms / 1000),
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as e:
            self._stream = None
            raise ErrorMicrofono(f"No se pudo abrir el micrófono '{info['name']}': {e}") from e
        log.info("Micrófono: %s (%d Hz)", info["name"], self._frecuencia_captura)

    def _callback(self, indata, frames, tiempo, estado) -> None:
        """Lo llama sounddevice desde otro hilo con cada bloque: debe ser rápido."""
        if estado:
            log.debug("Estado del micrófono: %s", estado)   # p. ej. "input overflow"
        bloque = indata[:, 0].copy()   # copia: sounddevice reutiliza el buffer
        try:
            self._cola.put_nowait(bloque)
        except queue.Full:
            try:
                self._cola.get_nowait()   # descarta el más antiguo
            except queue.Empty:
                pass
            self._cola.put_nowait(bloque)

    # --- Lectura de audio ---------------------------------------------------

    def _leer_bloque(self) -> np.ndarray:
        if self._stream is None:
            raise ErrorMicrofono("El micrófono no está abierto: llama a iniciar().")
        try:
            bloque = self._cola.get(timeout=self._cfg.timeout_sin_audio_s)
        except queue.Empty:
            raise ErrorMicrofono(
                f"El micrófono no envía audio desde hace {self._cfg.timeout_sin_audio_s:.0f} s "
                "(¿desconectado o en uso por otro programa?)."
            ) from None
        return remuestrear(bloque, self._frecuencia_captura, self._cfg.frecuencia_muestreo)

    def descartar_audio(self) -> None:
        """Vacía la cola y olvida frases a medias (llamar tras hablar por el altavoz)."""
        while True:
            try:
                self._cola.get_nowait()
            except queue.Empty:
                break
        self._segmentador.reiniciar()

    def calibrar(self) -> float:
        """
        Mide el ruido ambiente durante `duracion_calibracion_s` y ajusta el umbral.
        Conviene estar en silencio mientras tanto. Devuelve el umbral nuevo.
        """
        self.descartar_audio()
        n = max(1, int(self._cfg.duracion_calibracion_s / self._bloque_s))
        niveles = [energia_rms(self._leer_bloque()) for _ in range(n)]
        ruido = float(np.median(niveles))   # la mediana ignora un golpe aislado
        umbral = max(self._cfg.umbral_energia_rms, ruido * self._cfg.factor_sobre_ruido)
        self._segmentador.umbral = umbral
        log.info("Ruido ambiente RMS=%.4f -> umbral de voz=%.4f", ruido, umbral)
        return umbral

    # --- API principal ------------------------------------------------------

    def escuchar(self, timeout: float | None = None) -> Transcripcion | None:
        """
        Espera una frase y la transcribe.

        timeout: segundos máximos esperando a que EMPIECE una frase (si ya
        empezó, se espera a que termine). None = esperar indefinidamente.

        Devuelve None si se agotó el tiempo sin oír nada. El ruido y las
        alucinaciones de Whisper se descartan y se sigue escuchando.
        Lanza ErrorMicrofono si el micrófono falla y ErrorSTT si falla Whisper.
        """
        limite = time.monotonic() + timeout if timeout is not None else None
        while True:
            audio = self._segmentador.procesar(self._leer_bloque())
            if audio is not None:
                resultado = self.transcribir(audio)
                if resultado is not None:
                    return resultado
            if limite is not None and not self._segmentador.grabando and time.monotonic() >= limite:
                return None

    def transcribir(self, audio: np.ndarray) -> Transcripcion | None:
        """Transcribe audio float32 a 16 kHz. Devuelve None si no hay texto útil."""
        if self._modelo is None:
            self._cargar_modelo()
        t0 = time.perf_counter()
        try:
            segmentos, _info = self._modelo.transcribe(
                audio,
                language=self._cfg.idioma,
                beam_size=self._cfg.beam_size,
                vad_filter=self._cfg.vad_filter,
                initial_prompt=self._cfg.prompt_inicial,
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            # `segmentos` es un generador: la transcripción ocurre al recorrerlo.
            partes = [s.text for s in segmentos if not self._es_alucinacion(s)]
        except Exception as e:
            raise ErrorSTT(f"Fallo al transcribir: {e}") from e

        tiempo = time.perf_counter() - t0
        texto = limpiar_texto(" ".join(partes), self._cfg.prompt_inicial)
        duracion = audio.size / self._cfg.frecuencia_muestreo
        if not texto:
            log.debug("Audio de %.1f s sin texto útil (%.2f s)", duracion, tiempo)
            return None
        return Transcripcion(texto, duracion, tiempo)

    def _es_alucinacion(self, segmento) -> bool:
        """Mismo criterio que Whisper: mucha probabilidad de no-voz y poca confianza."""
        return (segmento.no_speech_prob > self._cfg.max_prob_no_voz
                and segmento.avg_logprob < self._cfg.min_logprob_medio)


def listar_microfonos() -> str:
    """Texto con los dispositivos de entrada disponibles (para elegir CASA_MIC_INDICE)."""
    if sd is None:
        return "sounddevice no está instalado."
    lineas = []
    for indice, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            lineas.append(f"  [{indice}] {d['name']} ({int(d['default_samplerate'])} Hz)")
    try:
        predeterminado = sd.default.device[0]
    except Exception:
        predeterminado = None
    cabecera = f"Micrófonos (predeterminado: {predeterminado}):"
    return "\n".join([cabecera, *lineas]) if lineas else "No se encontró ningún micrófono."


# ---------------------------------------------------------------------------
# Prueba manual: python stt.py [--microfonos]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if "--microfonos" in sys.argv:
        print(listar_microfonos())
        raise SystemExit(0)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        with ReconocedorVoz() as reconocedor:
            print("Habla cerca del micrófono (Ctrl+C para salir).")
            while True:
                r = reconocedor.escuchar()
                print(f"> {r.texto}   [audio {r.duracion_audio_s:.1f} s, "
                      f"Whisper {r.tiempo_transcripcion_s:.2f} s]")
    except (ErrorMicrofono, ErrorSTT) as e:
        raise SystemExit(f"Error: {e}")
    except KeyboardInterrupt:
        print()
