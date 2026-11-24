"""
Pruebas de tts.py con un motor de voz simulado (no suena nada).

    python -m unittest tests.test_tts -v
"""

import contextlib
import dataclasses
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))

import tts  # noqa: E402
from config import TTS  # noqa: E402


class Voz:
    def __init__(self, id, name, languages=()):
        self.id, self.name, self.languages = id, name, list(languages)


# Voces reales típicas de Windows (SAPI5) y Linux (eSpeak).
ZIRA = Voz(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_ZIRA_11.0",
           "Microsoft Zira Desktop - English (United States)")
DAVID = Voz(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_DAVID_11.0",
            "Microsoft David Desktop - English (United States)")
SABINA = Voz(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_ES-MX_SABINA_11.0",
             "Microsoft Sabina Desktop - Spanish (Mexico)")
ESPEAK_ES = Voz("spanish", "spanish", [b"\x05es"])
ESPEAK_EN = Voz("english", "english", [b"\x05en"])


class MotorFalso:
    """Imita el motor de pyttsx3."""

    def __init__(self, voces, fallos=0):
        self.voces, self.fallos = voces, fallos
        self.propiedades, self.dicho = {}, []

    def setProperty(self, nombre, valor):
        self.propiedades[nombre] = valor

    def getProperty(self, nombre):
        return self.voces if nombre == "voices" else self.propiedades.get(nombre)

    def say(self, texto):
        self.dicho.append(texto)

    def runAndWait(self):
        if self.fallos:
            self.fallos -= 1
            raise RuntimeError("run loop already started")


class TestElegirVoz(unittest.TestCase):

    def test_windows(self):
        self.assertIs(tts.elegir_voz([ZIRA, DAVID, SABINA], TTS.palabras_clave_voz), SABINA)

    def test_espeak(self):
        self.assertIs(tts.elegir_voz([ESPEAK_EN, ESPEAK_ES], TTS.palabras_clave_voz), ESPEAK_ES)

    def test_sin_voz_en_espanol(self):
        self.assertIsNone(tts.elegir_voz([ZIRA, DAVID, ESPEAK_EN], TTS.palabras_clave_voz))
        self.assertIsNone(tts.elegir_voz([], TTS.palabras_clave_voz))


class TestSintetizador(unittest.TestCase):

    def test_configura_motor_y_habla(self):
        motor = MotorFalso([ZIRA, SABINA])
        s = tts.Sintetizador(fabrica_motor=lambda: motor)
        self.assertEqual(motor.propiedades["voice"], SABINA.id)
        self.assertEqual(motor.propiedades["rate"], TTS.velocidad)
        self.assertEqual(s.nombre_voz, SABINA.name)
        s.hablar("Luces encendidas")
        self.assertEqual(motor.dicho, ["Luces encendidas"])

    def test_texto_vacio_no_hace_nada(self):
        motor = MotorFalso([SABINA])
        s = tts.Sintetizador(fabrica_motor=lambda: motor)
        s.hablar("   ")
        s.hablar(None)
        self.assertEqual(motor.dicho, [])

    def test_reinicia_motor_si_falla(self):
        motores = [MotorFalso([SABINA], fallos=1), MotorFalso([SABINA])]
        s = tts.Sintetizador(fabrica_motor=lambda: motores.pop(0))
        s.hablar("Hola")
        self.assertEqual(motores, [])   # se creó un segundo motor
        self.assertFalse(s.modo_texto)

    def test_modo_texto_si_no_hay_motor(self):
        def fabrica_rota():
            raise OSError("SAPI5 no disponible")

        s = tts.Sintetizador(fabrica_motor=fabrica_rota)
        self.assertTrue(s.modo_texto)
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            s.hablar("Ventilador apagado")
        self.assertIn("Ventilador apagado", salida.getvalue())

    def test_imprime_si_el_motor_falla_siempre(self):
        s = tts.Sintetizador(fabrica_motor=lambda: MotorFalso([SABINA], fallos=99))
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            s.hablar("Hola")
        self.assertIn("Hola", salida.getvalue())

    def test_reiniciar_motor_cada_frase(self):
        creados = []

        def fabrica():
            creados.append(MotorFalso([SABINA]))
            return creados[-1]

        cfg = dataclasses.replace(TTS, reiniciar_motor_cada_frase=True)
        s = tts.Sintetizador(cfg, fabrica_motor=fabrica)
        s.hablar("uno")
        s.hablar("dos")
        self.assertEqual(len(creados), 3)   # 1 al iniciar + 1 por frase
        self.assertEqual(creados[-1].dicho, ["dos"])


if __name__ == "__main__":
    unittest.main()
