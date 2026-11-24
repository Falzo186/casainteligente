"""
Pruebas del diálogo (dialogo.py) sin micrófono ni altavoces: el entendedor es
el real (con un Ollama simulado) y la casa es la placa simulada por serie, así
se prueba el flujo completo frase -> comando -> placa -> respuesta hablada.

    python -m unittest tests.test_dialogo -v
"""

import dataclasses
import queue
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "pc"))
sys.path.insert(0, str(RAIZ / "tests"))

import dialogo  # noqa: E402
import entendedor as ent  # noqa: E402
import serial_client as sc  # noqa: E402
from acciones import Ejecutor  # noqa: E402
from config import DIALOGO, SERIE  # noqa: E402
from simulador_casa import CasaSimulada  # noqa: E402
from stt import ErrorMicrofono  # noqa: E402
from test_nlu import CATALOGO, OllamaFalso, salida  # noqa: E402

CFG = dataclasses.replace(SERIE, puerto="auto", timeout_lectura_s=0.02, timeout_respuesta_s=0.3, espera_arranque_s=0.6)


class FuenteFalsa:
    """Imita a FuenteVoz: entrega frases (o errores) predefinidos; None = un tramo en silencio."""

    def __init__(self, *entradas):
        self.cola = list(entradas)
        self.timeouts, self.descartes, self.recuperaciones = [], 0, 0

    def escuchar(self, timeout):
        self.timeouts.append(timeout)
        if not self.cola:
            return None
        item = self.cola.pop(0)
        if item is None:
            return None
        if isinstance(item, Exception):
            raise item
        return dialogo.Entrada(item, 1.0, 0.5)

    def descartar(self):
        self.descartes += 1

    def recuperar(self):
        self.recuperaciones += 1


class TTSFalso:
    def __init__(self):
        self.dicho = []

    def hablar(self, texto):
        self.dicho.append(texto)


class Base(unittest.TestCase):

    def setUp(self):
        self.placa = CasaSimulada()
        self.cliente = sc.ClienteSerie(CFG, fabrica_puerto=self.placa.abrir)
        self.cliente.conectar()
        self.addCleanup(self.cliente.cerrar)
        self.alertas = queue.Queue()
        self.eventos = []

    def crear(self, frases, respuestas_llm=(), llm_falla=False):
        self.fuente = FuenteFalsa(*frases)
        self.tts = TTSFalso()
        self.ollama = OllamaFalso(*respuestas_llm, falla=llm_falla)
        entendedor = ent.Entendedor(CATALOGO, self.ollama)
        return dialogo.Asistente(
            self.fuente, self.tts, entendedor, Ejecutor(self.cliente, CATALOGO), self.alertas,
            al_evento=lambda tipo, texto, **meta: self.eventos.append((tipo, texto)),
            timeout_respuesta_s=0.05, pausa_microfono_s=0,
        )


class TestOrdenes(Base):

    def test_comando_directo(self):
        a = self.crear(["prende la luz de la sala"])
        a.ciclo()
        self.assertTrue(self.placa.leds[3])
        self.assertEqual(self.tts.dicho, ["Luces de la sala encendidas."])
        self.assertEqual(self.fuente.descartes, 1)                 # descartó el audio tras hablar
        self.assertEqual(self.ollama.llamadas, [])                 # las reglas no llaman al modelo

    def test_musica_y_volumen(self):
        a = self.crear(["pon thriller", "sube el volumen", "para la música"])
        for _ in range(3):
            a.ciclo()
        self.assertEqual(self.tts.dicho, ["Reproduciendo Thriller.", "Volumen al 60 por ciento.", "Música detenida."])
        self.assertEqual(self.placa.pista, 0)

    def test_consulta(self):
        a = self.crear(["qué temperatura hace"])
        a.ciclo()
        self.assertEqual(self.tts.dicho, ["La temperatura es de 24,5 grados."])

    def test_seguridad(self):
        a = self.crear(["activa el modo guardián", "apaga la alarma"])
        a.ciclo()
        self.assertTrue(self.placa.guardian)
        self.placa.disparar_intruso()
        a.ciclo()
        self.assertFalse(self.placa.alarma)

    def test_frase_desconocida(self):
        a = self.crear(["cuéntame un chiste"], [salida("desconocido", "ninguno")])
        a.ciclo()
        self.assertEqual(self.tts.dicho, [DIALOGO.mensaje_no_entendido])

    def test_dos_ordenes_a_la_vez(self):
        a = self.crear(["prende la sala y el ventilador"])
        a.ciclo()
        self.assertEqual(self.tts.dicho, ["Dime una orden a la vez, por favor."])
        self.assertFalse(self.placa.ventilador)


class TestAclaraciones(Base):

    def test_se_fusiona(self):
        a = self.crear(["prende las luces", "en el comedor"])
        a.ciclo()
        self.assertIsNotNone(a.pendiente)
        self.assertIn("¿Qué luces quieres encender", self.tts.dicho[0])
        self.assertFalse(any(self.placa.leds))                    # nada se ejecuta todavía
        a.ciclo()
        self.assertTrue(self.placa.leds[2])
        self.assertEqual(self.tts.dicho[1], "Luces del comedor encendidas.")
        self.assertIsNone(a.pendiente)

    def test_cambia_de_idea(self):
        a = self.crear(["prende las luces", "mejor el ventilador"])
        a.ciclo()
        a.ciclo()
        self.assertTrue(self.placa.ventilador)
        self.assertFalse(any(self.placa.leds))

    def test_cancelar(self):
        a = self.crear(["prende las luces", "¡Olvídalo!"])
        a.ciclo()
        a.ciclo()
        self.assertEqual(self.tts.dicho[-1], DIALOGO.mensaje_cancelado)
        self.assertIsNone(a.pendiente)
        self.assertFalse(any(self.placa.leds))

    def test_cancelar_sin_pendiente_no_es_cancelar(self):
        a = self.crear(["nada"], [salida("desconocido", "ninguno")])
        a.ciclo()
        self.assertEqual(self.tts.dicho, [DIALOGO.mensaje_no_entendido])

    def test_sin_respuesta(self):
        a = self.crear(["prende las luces"])
        a.ciclo()
        a.ciclo()                                                  # nadie responde: caduca
        self.assertEqual(self.tts.dicho[-1], DIALOGO.mensaje_sin_respuesta)
        self.assertIsNone(a.pendiente)

    def test_respuestas_inutiles_se_agotan(self):
        a = self.crear(["prende las luces", "no sé", "mmm"])
        a.ciclo()
        a.ciclo()                                                  # 1.er intento fallido: repite la pregunta
        self.assertIn("¿Qué luces quieres encender", self.tts.dicho[1])
        self.assertIsNotNone(a.pendiente)
        a.ciclo()                                                  # 2.º: cancela
        self.assertEqual(self.tts.dicho[2], DIALOGO.mensaje_sin_aclaracion)
        self.assertIsNone(a.pendiente)

    def test_aclaracion_caducada_es_orden_nueva(self):
        a = self.crear(["prende las luces", "enciende el ventilador"])
        a.ciclo()
        a.pendiente_desde -= DIALOGO.timeout_aclaracion_s + 1
        a.ciclo()
        self.assertTrue(self.placa.ventilador)

    def test_musica_sin_titulo_pregunta(self):
        a = self.crear(["pon música", "la dos"])
        a.ciclo()
        self.assertIn("¿Qué canción quieres escuchar?", self.tts.dicho[0])
        a.ciclo()
        self.assertEqual(self.placa.pista, 3)                      # la 2.ª del catálogo es la pista 3

    def test_el_modelo_pide_confirmacion(self):
        a = self.crear(["cierra la puerta", "no"], [salida("apagar", "guardian")])
        self.placa.guardian = True
        a.ciclo()
        self.assertEqual(self.tts.dicho[0], "¿Quieres que desactive el modo guardián?")
        self.assertTrue(self.placa.guardian)                       # no se ejecutó
        a.ciclo()
        self.assertEqual(self.tts.dicho[1], "De acuerdo, no hago nada.")
        self.assertTrue(self.placa.guardian)
        self.assertIsNone(a.pendiente)

    def test_confirmar_con_si(self):
        a = self.crear(["refréscame un poco", "sí"], [salida("encender", "ventilador")])
        a.ciclo()
        self.assertEqual(self.tts.dicho[0], "¿Quieres que encienda el ventilador?")
        a.ciclo()
        self.assertTrue(self.placa.ventilador)


class TestErrores(Base):

    def test_placa_sin_respuesta(self):
        a = self.crear(["prende la sala"])
        self.placa.silenciosa = True
        a.ciclo()
        self.assertEqual(self.tts.dicho, [DIALOGO.mensaje_error_red])

    def test_la_placa_rechaza(self):
        self.placa.alarma = True
        a = self.crear(["pon thriller"])
        a.ciclo()
        self.assertEqual(self.tts.dicho, ["La alarma está sonando. Apágala primero."])

    def test_ollama_caido_no_rompe_las_reglas(self):
        a = self.crear(["prende la sala", "déjame ver en el baño"], llm_falla=True)
        a.ciclo()
        a.ciclo()
        self.assertTrue(self.placa.leds[3])
        self.assertEqual(self.tts.dicho[1], DIALOGO.mensaje_no_entendido)

    def test_microfono_se_recupera(self):
        a = self.crear([ErrorMicrofono("sin audio"), "enciende el ventilador"])
        a.ciclo()
        self.assertEqual(self.fuente.recuperaciones, 1)
        self.assertEqual(self.tts.dicho, [DIALOGO.mensaje_sin_microfono])
        a.ciclo()
        self.assertTrue(self.placa.ventilador)

    def test_microfono_tarda_en_volver(self):
        a = self.crear([ErrorMicrofono("sin audio")])
        intentos = []

        def recuperar():
            intentos.append(1)
            if len(intentos) < 3:
                raise ErrorMicrofono("sigue sin micrófono")

        self.fuente.recuperar = recuperar
        a.ciclo()
        self.assertEqual(len(intentos), 3)
        self.assertEqual(self.tts.dicho, [DIALOGO.mensaje_sin_microfono])     # avisa una sola vez


class TestEventosYTiempos(Base):

    def test_eventos_para_la_interfaz(self):
        a = self.crear(["prende la sala"])
        with self.assertLogs("tiempos", "INFO") as capturado:
            a.ciclo()
        tipos = [t for t, _ in self.eventos]
        self.assertEqual(tipos[:2], ["oi", "dije"])
        self.assertIn("tiempos", tipos)
        linea = capturado.output[0]
        for etapa in ("STT", "reglas", "placa", "voz", "total"):
            self.assertIn(etapa, linea)

    def test_alertas_juntas_y_sin_repetir(self):
        a = self.crear([])
        self.alertas.put("¡Alerta de seguridad! Se detectó movimiento.")
        self.alertas.put("¡Alerta de seguridad! Se detectó movimiento.")
        self.alertas.put("Otra cosa.")
        a.ciclo()
        self.assertEqual(self.tts.dicho, ["¡Alerta de seguridad! Se detectó movimiento. Otra cosa."])
        self.assertIn(("alerta", "¡Alerta de seguridad! Se detectó movimiento. Otra cosa."), self.eventos)

    def test_alerta_durante_una_aclaracion_no_la_pierde(self):
        a = self.crear(["prende las luces", None, "en el cuarto"])
        a.ciclo()
        self.alertas.put("¡Alerta de seguridad! Se detectó movimiento.")
        a.ciclo()
        self.assertIn("movimiento", self.tts.dicho[1])
        self.assertTrue(self.placa.leds[4])
        self.assertEqual(self.tts.dicho[2], "Luces del cuarto encendidas.")


class TestFuentes(unittest.TestCase):

    def test_es_cancelacion(self):
        for frase in ("cancela", "Olvídalo", "¡Déjalo!", "nada, gracias", "cancelar por favor"):
            with self.subTest(frase=frase):
                self.assertTrue(dialogo.es_cancelacion(frase))
        for frase in ("en la sala", "no hagas nada", "la del patio"):
            with self.subTest(frase=frase):
                self.assertFalse(dialogo.es_cancelacion(frase))

    def test_fuente_teclado(self):
        f = dialogo.FuenteTeclado()
        self.assertIsNone(f.escuchar(0.01))
        f._cola.put("prende la sala")
        self.assertEqual(f.escuchar(0.01).texto, "prende la sala")
        f._cola.put(None)
        with self.assertRaises(dialogo.FinDeEntrada):
            f.escuchar(0.01)

    def test_fuente_combinada(self):
        voz = FuenteFalsa("pon thriller")
        f = dialogo.FuenteCombinada()
        f.enviar_texto("  ")
        self.assertIsNone(f.escuchar(0.01))
        f.enviar_texto("prende la sala")
        self.assertEqual((f.escuchar(0.01).texto, f.escuchar(0.01)), ("prende la sala", None))
        f.voz = voz                                               # se activa el micrófono en caliente
        f.enviar_texto("apaga todo")
        self.assertEqual(f.escuchar(0.5).texto, "apaga todo")      # el texto escrito tiene prioridad
        self.assertEqual(f.escuchar(0.5).texto, "pon thriller")
        self.assertLessEqual(voz.timeouts[-1], 0.25)               # escucha en tramos cortos
        f.descartar()
        self.assertEqual(voz.descartes, 1)


if __name__ == "__main__":
    unittest.main()
