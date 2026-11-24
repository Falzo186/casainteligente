"""
tts.py — Síntesis de voz offline con pyttsx3, en español.

pyttsx3 usa el motor de voz del sistema operativo:
  - Windows: SAPI5 (voces "Microsoft Helena/Sabina/Laura" si están instaladas).
  - Linux: eSpeak (sudo apt install espeak-ng).
  - macOS: NSSpeechSynthesizer.

Si no hay motor de voz disponible, el sintetizador pasa a "modo texto":
imprime las respuestas en consola y el resto del sistema sigue funcionando.

Prueba manual:

    python tts.py            # lista las voces y dice una frase de prueba
    python tts.py "Hola"     # dice el texto indicado
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from typing import Any, Callable

from config import TTS, ConfigTTS

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

log = logging.getLogger("tts")


def _palabras(texto: str) -> set[str]:
    """Separa un texto en palabras normalizadas: 'TTS_MS_ES-MX_SABINA' -> {tts, ms, es, mx, sabina}."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return set(re.split(r"[^a-z0-9]+", sin_tildes)) - {""}


def _descripcion_voz(voz: Any) -> str:
    """Junta id, nombre e idiomas de una voz de pyttsx3 en un solo texto."""
    idiomas = []
    for idioma in getattr(voz, "languages", None) or []:
        if isinstance(idioma, bytes):   # eSpeak devuelve p. ej. b"\x05es"
            idioma = idioma.decode("utf-8", errors="ignore")
        idiomas.append(str(idioma))
    return " ".join([str(getattr(voz, "id", "")), str(getattr(voz, "name", "")), *idiomas])


def elegir_voz(voces: list[Any], palabras_clave: tuple[str, ...]) -> Any | None:
    """
    Devuelve la primera voz cuyo id/nombre/idioma contenga alguna de las
    palabras clave como palabra completa (así "es" no coincide con "tests").
    None si ninguna coincide.
    """
    claves = set().union(*(_palabras(p) for p in palabras_clave)) if palabras_clave else set()
    for voz in voces or []:
        if _palabras(_descripcion_voz(voz)) & claves:
            return voz
    return None


class Sintetizador:
    """
    Uso:
        tts = Sintetizador()
        tts.hablar("Luces de la sala encendidas")   # bloquea hasta terminar
    """

    def __init__(self, cfg: ConfigTTS = TTS, fabrica_motor: Callable[[], Any] | None = None) -> None:
        """`fabrica_motor` permite inyectar un motor falso en los tests (por defecto, pyttsx3.init)."""
        self._cfg = cfg
        self._fabrica = fabrica_motor or (pyttsx3.init if pyttsx3 is not None else None)
        self._lock = threading.Lock()   # pyttsx3 no admite dos frases a la vez
        self._motor: Any = None
        self.nombre_voz: str | None = None
        self.modo_texto = False
        self._crear_motor()

    # --- Motor --------------------------------------------------------------

    def _crear_motor(self) -> bool:
        """Crea y configura el motor. Si no es posible, activa el modo texto."""
        if self._fabrica is None:
            log.warning("pyttsx3 no está instalado: las respuestas solo se mostrarán en consola.")
            self.modo_texto = True
            return False
        try:
            motor = self._fabrica()
            motor.setProperty("rate", self._cfg.velocidad)
            motor.setProperty("volume", self._cfg.volumen)
            voz = elegir_voz(motor.getProperty("voices"), self._cfg.palabras_clave_voz)
            if voz is not None:
                motor.setProperty("voice", voz.id)
                self.nombre_voz = getattr(voz, "name", str(voz.id))
            elif self.nombre_voz is None:   # avisar solo la primera vez
                log.warning(
                    "No hay ninguna voz en español instalada; se usará la voz por defecto. "
                    "En Windows: Configuración > Hora e idioma > Voz > Agregar voces > Español."
                )
        except Exception as e:
            log.warning("No se pudo iniciar el motor de voz (%s): se usará modo texto.", e)
            self._motor = None
            self.modo_texto = True
            return False
        self._motor = motor
        self.modo_texto = False
        return True

    # --- API principal ------------------------------------------------------

    def hablar(self, texto: str) -> None:
        """
        Dice el texto y espera a que termine. Nunca lanza excepciones: si el
        motor falla, se reinicia una vez y, si sigue fallando, se imprime el
        texto en consola.
        """
        texto = (texto or "").strip()
        if not texto:
            return
        log.info("Diciendo: %s", texto)

        with self._lock:
            if self.modo_texto:
                print(f"[voz] {texto}")
                return

            for intento in (1, 2):
                if self._cfg.reiniciar_motor_cada_frase and intento == 1:
                    self._crear_motor()
                if self._motor is None:
                    break
                try:
                    self._motor.say(texto)
                    self._motor.runAndWait()
                    return
                except Exception as e:
                    log.warning("Fallo del motor de voz (intento %d): %s", intento, e)
                    if intento == 1 and not self._crear_motor():
                        break
            print(f"[voz] {texto}")

    def listar_voces(self) -> list[str]:
        """Descripciones de las voces instaladas (para elegir palabras_clave_voz)."""
        if self._motor is None:
            return []
        return [_descripcion_voz(v) for v in self._motor.getProperty("voices") or []]


# ---------------------------------------------------------------------------
# Prueba manual: python tts.py ["texto"]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    tts = Sintetizador()
    print("Voces instaladas:")
    for descripcion in tts.listar_voces():
        print(f"  - {descripcion}")
    print(f"Voz elegida: {tts.nombre_voz or '(por defecto)'}"
          f"{'  [MODO TEXTO]' if tts.modo_texto else ''}")
    tts.hablar(" ".join(sys.argv[1:]) or "Hola, soy tu casa inteligente. ¿Qué luces quieres encender?")
