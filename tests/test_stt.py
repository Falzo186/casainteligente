"""
Pruebas de stt.py sin micrófono ni Whisper: el audio se genera con numpy.

    python -m unittest tests.test_stt -v
"""

import dataclasses
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))

try:
    import numpy as np
    import stt
    from config import STT
except ImportError as e:  # numpy no instalado
    raise unittest.SkipTest(f"falta una dependencia: {e}")

MUESTRAS = int(STT.frecuencia_muestreo * STT.duracion_bloque_ms / 1000)
BLOQUE_S = MUESTRAS / STT.frecuencia_muestreo
UMBRAL = 0.05


def voz(amplitud=0.3):
    """Un bloque de 'voz': seno de 440 Hz (RMS = amplitud / √2)."""
    t = np.arange(MUESTRAS) / STT.frecuencia_muestreo
    return (amplitud * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def silencio():
    return np.zeros(MUESTRAS, dtype=np.float32)


def alimentar(segmentador, bloques):
    """Pasa los bloques y devuelve (índice, audio) de la primera frase completa, o (None, None)."""
    for i, bloque in enumerate(bloques):
        audio = segmentador.procesar(bloque)
        if audio is not None:
            return i, audio
    return None, None


class TestUtilidades(unittest.TestCase):

    def test_energia_rms(self):
        self.assertEqual(stt.energia_rms(silencio()), 0.0)
        self.assertAlmostEqual(stt.energia_rms(voz(0.3)), 0.3 / np.sqrt(2), places=2)
        self.assertEqual(stt.energia_rms(np.array([], dtype=np.float32)), 0.0)

    def test_remuestrear(self):
        audio = np.random.default_rng(0).uniform(-1, 1, 1440).astype(np.float32)
        salida = stt.remuestrear(audio, 48000, 16000)
        self.assertEqual(salida.size, 480)
        self.assertEqual(salida.dtype, np.float32)
        self.assertIs(stt.remuestrear(audio, 16000, 16000), audio)


class TestLimpiarTexto(unittest.TestCase):

    def test_texto_normal(self):
        self.assertEqual(stt.limpiar_texto("  Prende   la luz de la sala. "), "Prende la luz de la sala.")

    def test_alucinaciones(self):
        for texto in ("Subtítulos realizados por la comunidad de Amara.org",
                      "¡Gracias por ver el video!",
                      "Suscríbete al canal",
                      "...", "¿?", ""):
            with self.subTest(texto=texto):
                self.assertEqual(stt.limpiar_texto(texto, STT.prompt_inicial), "")

    def test_eco_del_prompt(self):
        self.assertEqual(stt.limpiar_texto(STT.prompt_inicial, STT.prompt_inicial), "")
        self.assertEqual(stt.limpiar_texto("Comandos de casa: enciende", STT.prompt_inicial), "")
        # Una orden corta que comparte palabras con el prompt NO se descarta.
        self.assertEqual(stt.limpiar_texto("enciende", STT.prompt_inicial), "enciende")


class TestSegmentador(unittest.TestCase):

    def setUp(self):
        self.seg = stt.SegmentadorVoz(UMBRAL, BLOQUE_S, STT)

    def test_silencio_no_produce_frases(self):
        indice, _ = alimentar(self.seg, [silencio()] * 200)
        self.assertIsNone(indice)
        self.assertFalse(self.seg.grabando)

    def test_frase_completa_con_preroll(self):
        bloques = [silencio()] * 5 + [voz()] * 20 + [silencio()] * 60
        indice, audio = alimentar(self.seg, bloques)
        self.assertIsNotNone(audio)
        # Termina tras ~silencio_fin_frase_s de silencio.
        silencios = indice - 24
        self.assertAlmostEqual(silencios * BLOQUE_S, STT.silencio_fin_frase_s, delta=BLOQUE_S)
        # Incluye los 5 bloques previos (pre-roll), la voz y el silencio final.
        self.assertEqual(audio.size, (5 + 20 + silencios) * MUESTRAS)
        self.assertFalse(self.seg.grabando)

    def test_clic_corto_se_descarta(self):
        indice, _ = alimentar(self.seg, [voz()] * 3 + [silencio()] * 60)
        self.assertIsNone(indice)

    def test_ruido_aislado_no_dispara(self):
        # Voz y silencio alternados: nunca hay bloques_inicio_voz seguidos.
        indice, _ = alimentar(self.seg, [voz(), silencio()] * 50)
        self.assertIsNone(indice)
        self.assertFalse(self.seg.grabando)

    def test_duracion_maxima(self):
        indice, audio = alimentar(self.seg, [voz()] * 1000)
        self.assertIsNotNone(audio)
        self.assertAlmostEqual(audio.size / STT.frecuencia_muestreo, STT.duracion_max_frase_s, delta=0.1)

    def test_histeresis_mantiene_voz_suave(self):
        # Ya grabando, una voz por debajo del umbral pero sobre umbral*histeresis
        # no cuenta como silencio.
        suave = voz(UMBRAL * 0.8 * np.sqrt(2))   # RMS = 0.8 * umbral
        bloques = [voz()] * 10 + [suave] * 40 + [silencio()] * 60
        indice, _ = alimentar(self.seg, bloques)
        self.assertGreaterEqual(indice, 50)

    def test_reiniciar_olvida_frase(self):
        alimentar(self.seg, [voz()] * 10)
        self.assertTrue(self.seg.grabando)
        self.seg.reiniciar()
        self.assertFalse(self.seg.grabando)


class TestReconocedorSinHardware(unittest.TestCase):
    """Se simula el micrófono llenando la cola a mano y se sustituye Whisper."""

    def crear(self, **cambios):
        cfg = dataclasses.replace(STT, **cambios)
        r = stt.ReconocedorVoz(cfg)
        r._stream = object()   # "micrófono abierto"
        return r

    def llenar(self, r, bloques):
        for b in bloques:
            r._cola.put_nowait(b)

    def test_escuchar_devuelve_transcripcion(self):
        r = self.crear()
        r._segmentador.umbral = UMBRAL
        recibido = []

        def transcribir_falso(audio):
            recibido.append(audio)
            return stt.Transcripcion("prende la sala", audio.size / 16000, 0.01)

        r.transcribir = transcribir_falso
        self.llenar(r, [silencio()] * 3 + [voz()] * 20 + [silencio()] * 40)
        resultado = r.escuchar()
        self.assertEqual(resultado.texto, "prende la sala")
        self.assertEqual(len(recibido), 1)

    def test_escuchar_sigue_si_whisper_no_entiende(self):
        r = self.crear()
        r._segmentador.umbral = UMBRAL
        respuestas = [None, stt.Transcripcion("apaga el ventilador", 1.0, 0.01)]
        r.transcribir = lambda audio: respuestas.pop(0)
        frase = [voz()] * 20 + [silencio()] * 40
        self.llenar(r, frase + frase)
        self.assertEqual(r.escuchar().texto, "apaga el ventilador")

    def test_timeout_sin_voz(self):
        r = self.crear()
        self.llenar(r, [silencio()] * 5)
        self.assertIsNone(r.escuchar(timeout=0))

    def test_microfono_sin_audio(self):
        r = self.crear(timeout_sin_audio_s=0.05)
        with self.assertRaises(stt.ErrorMicrofono):
            r.escuchar()

    def test_microfono_no_abierto(self):
        r = stt.ReconocedorVoz()
        with self.assertRaises(stt.ErrorMicrofono):
            r.escuchar()

    def test_calibrar_sube_umbral_con_ruido(self):
        r = self.crear(duracion_calibracion_s=0.3)
        ruido = voz(0.02 * np.sqrt(2))   # RMS 0.02 -> umbral = 0.02 * factor_sobre_ruido
        self.llenar(r, [ruido] * 20)
        # calibrar() vacía la cola primero; se vuelve a llenar desde otro "hilo".
        r.descartar_audio = lambda: r._segmentador.reiniciar()
        umbral = r.calibrar()
        self.assertAlmostEqual(umbral, 0.02 * STT.factor_sobre_ruido, places=3)

    def test_calibrar_respeta_umbral_minimo(self):
        r = self.crear(duracion_calibracion_s=0.3)
        self.llenar(r, [silencio()] * 20)
        r.descartar_audio = lambda: r._segmentador.reiniciar()
        self.assertEqual(r.calibrar(), STT.umbral_energia_rms)

    def test_cola_llena_descarta_lo_mas_antiguo(self):
        r = self.crear()
        capacidad = r._cola.maxsize
        for i in range(capacidad + 5):
            r._callback(np.full((MUESTRAS, 1), i, dtype=np.float32), MUESTRAS, None, None)
        self.assertEqual(r._cola.qsize(), capacidad)
        self.assertEqual(r._cola.get_nowait()[0], 5)


if __name__ == "__main__":
    unittest.main()
