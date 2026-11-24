"""
Pruebas de serial_client.py contra la placa simulada (sin hardware).

    python -m unittest tests.test_serial_client -v
"""

import dataclasses
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))

import serial_client as sc  # noqa: E402
from config import SERIE  # noqa: E402
from simulador_casa import CasaSimulada  # noqa: E402

CFG = dataclasses.replace(SERIE, puerto="auto", timeout_lectura_s=0.02,
                          timeout_respuesta_s=0.3, espera_arranque_s=0.6)


def esperar(condicion, segundos=1.0):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.005)
    return condicion()


class Base(unittest.TestCase):

    def setUp(self):
        self.casa = CasaSimulada()
        self.cliente = sc.ClienteSerie(CFG, fabrica_puerto=self.casa.abrir)
        self.addCleanup(self.cliente.cerrar)
        self.eventos = []
        self.cliente.suscribir(self.eventos.append)
        self.cliente.conectar()


class TestOrdenes(Base):

    def test_conexion_con_handshake(self):
        self.assertTrue(self.cliente.conectado)
        self.assertEqual(self.casa.recibidas[0], "PING")

    def test_set_led(self):
        self.cliente.set_led(4, True)
        self.assertTrue(self.casa.leds[3])
        self.cliente.set_led(4, False)
        self.assertFalse(self.casa.leds[3])

    def test_orden_repetida_no_alterna(self):
        self.cliente.set_led(2, True)
        self.cliente.set_led(2, True)
        self.assertTrue(self.casa.leds[1])

    def test_led_invalido_no_envia(self):
        enviadas = len(self.casa.recibidas)
        for n in (0, 7):
            with self.assertRaises(ValueError):
                self.cliente.set_led(n, True)
        self.assertEqual(len(self.casa.recibidas), enviadas)

    def test_todos_ventilador_guardian(self):
        self.cliente.set_todos_los_leds(True)
        self.assertTrue(all(self.casa.leds))
        self.cliente.set_ventilador(True)
        self.assertTrue(self.casa.ventilador)
        self.cliente.set_guardian(True)
        self.assertTrue(self.casa.guardian)
        self.cliente.set_guardian(False)
        self.assertFalse(self.casa.guardian)

    def test_musica(self):
        self.cliente.reproducir(3)
        self.assertEqual(self.casa.pista, 3)
        self.cliente.set_volumen(99)            # se limita a 30
        self.assertEqual(self.casa.volumen, 30)
        self.cliente.detener()
        self.assertEqual(self.casa.pista, 0)
        with self.assertRaises(ValueError):
            self.cliente.reproducir(31)

    def test_estado(self):
        self.cliente.set_led(1, True)
        self.cliente.set_ventilador(True)
        self.cliente.reproducir(5)
        e = self.cliente.obtener_estado()
        self.assertEqual((e.temperatura, e.humedad), (24.5, 58.0))
        self.assertEqual(e.leds, (True, False, False, False, False, False))
        self.assertTrue(e.ventilador)
        self.assertEqual((e.pista, e.volumen), (5, 14))
        self.assertTrue(e.mp3)

    def test_estado_con_dht_sin_lectura(self):
        self.casa.temperatura = self.casa.humedad = None
        e = self.cliente.obtener_estado()
        self.assertIsNone(e.temperatura)
        self.assertIsNone(e.humedad)

    def test_err_de_la_placa(self):
        self.casa.alarma = True
        with self.assertRaises(sc.ErrorRespuestaCasa) as ctx:
            self.cliente.reproducir(2)
        self.assertIn("alarma", str(ctx.exception))

    def test_mp3_no_detectado(self):
        casa = CasaSimulada(con_mp3=False)
        with sc.ClienteSerie(CFG, fabrica_puerto=casa.abrir) as c:
            with self.assertRaises(sc.ErrorRespuestaCasa):
                c.reproducir(2)
            self.assertFalse(c.obtener_estado().mp3)


class TestEventos(Base):

    def test_intruso(self):
        self.cliente.set_guardian(True)
        self.casa.disparar_intruso()
        self.assertTrue(esperar(lambda: any(e.tipo == "intruso" for e in self.eventos)))
        e = self.cliente.obtener_estado()
        self.assertTrue(e.alarma)
        self.assertEqual((e.eventos, e.pista), (1, 1))     # suena la pista 1

    def test_sin_guardian_no_hay_evento(self):
        self.casa.disparar_intruso()
        time.sleep(0.1)
        self.assertEqual(self.eventos, [])

    def test_evento_entre_orden_y_respuesta(self):
        # Un evento no debe confundirse con la respuesta de una orden.
        self.cliente.set_guardian(True)
        self.casa.disparar_intruso()
        self.cliente.set_led(1, True)
        self.assertTrue(self.casa.leds[0])

    def test_fin_de_pista(self):
        self.cliente.reproducir(4)
        self.casa.terminar_pista()
        self.assertTrue(esperar(lambda: any(e.tipo == "fin" and e.dato == 4 for e in self.eventos)))

    def test_apagar_alarma(self):
        self.cliente.set_guardian(True)
        self.casa.disparar_intruso()
        self.cliente.apagar_alarma()
        e = self.cliente.obtener_estado()
        self.assertEqual((e.alarma, e.guardian, e.pista), (False, False, 0))

    def test_suscriptor_defectuoso_no_rompe_el_lector(self):
        self.cliente.suscribir(lambda ev: 1 / 0)
        self.cliente.set_guardian(True)
        self.casa.disparar_intruso()
        self.assertTrue(esperar(lambda: len(self.eventos) == 1))
        self.cliente.set_led(1, True)    # el lector sigue vivo


class TestErroresDeConexion(unittest.TestCase):

    def test_placa_muda(self):
        casa = CasaSimulada()
        casa.silenciosa = True
        c = sc.ClienteSerie(CFG, fabrica_puerto=casa.abrir)
        with self.assertRaises(sc.ErrorConexionCasa) as ctx:
            c.conectar()
        self.assertIn("no responde", str(ctx.exception))
        self.assertFalse(c.conectado)

    def test_timeout_de_una_orden(self):
        casa = CasaSimulada()
        with sc.ClienteSerie(CFG, fabrica_puerto=casa.abrir) as c:
            casa.silenciosa = True
            with self.assertRaises(sc.ErrorConexionCasa):
                c.set_led(1, True)
            self.assertTrue(c.conectado)    # un timeout aislado no cierra el puerto

    def test_desconexion_y_reconexion(self):
        casa = CasaSimulada()
        c = sc.ClienteSerie(CFG, fabrica_puerto=casa.abrir)
        eventos = []
        c.suscribir(eventos.append)
        c.conectar()
        casa.leds[2] = True                 # el estado de la placa sobrevive al cable
        casa.desenchufar()
        self.assertTrue(esperar(lambda: not c.conectado))
        self.assertEqual([e.tipo for e in eventos], ["desconexion"])
        # La siguiente orden vuelve a conectar sola (cable enchufado de nuevo).
        self.assertTrue(c.obtener_estado().leds[2])
        self.assertTrue(c.conectado)
        c.cerrar()

    def test_fallo_al_abrir(self):
        def fabrica(*args):
            raise PermissionError("acceso denegado")

        c = sc.ClienteSerie(CFG, fabrica_puerto=fabrica)
        with self.assertRaises(sc.ErrorConexionCasa) as ctx:
            c.conectar()
        self.assertIn("Monitor Serie", str(ctx.exception))

    def test_lineas_partidas_y_basura(self):
        casa = CasaSimulada()
        puerto_real = casa.abrir

        def fabrica(*args):
            puerto = puerto_real(*args)
            original = puerto.readline
            trozos = []

            def readline():
                # Parte cada línea en dos lecturas y mezcla basura del bootloader.
                if trozos:
                    return trozos.pop(0)
                linea = original()
                if linea and len(linea) > 3:
                    trozos.append(linea[3:])
                    return b"\xff\xfe garbage\n" + linea[:3]
                return linea

            puerto.readline = readline
            return puerto

        with sc.ClienteSerie(CFG, fabrica_puerto=fabrica) as c:
            c.set_led(3, True)
            self.assertTrue(c.obtener_estado().leds[2])


class TestElegirPuerto(unittest.TestCase):

    def test_prefiere_chips_de_placas(self):
        puertos = [SimpleNamespace(device="COM1", vid=None, description="Puerto de comunicaciones"),
                   SimpleNamespace(device="COM7", vid=0x10C4, description="Silicon Labs CP210x")]
        self.assertEqual(sc.elegir_puerto(puertos), "COM7")

    def test_unico_puerto(self):
        self.assertEqual(sc.elegir_puerto([SimpleNamespace(device="COM3", vid=None, description="x")]), "COM3")

    def test_sin_puertos(self):
        with self.assertRaises(sc.ErrorConexionCasa) as ctx:
            sc.elegir_puerto([])
        self.assertIn("USB", str(ctx.exception))

    def test_varios_sin_identificar(self):
        puertos = [SimpleNamespace(device=f"COM{i}", vid=None, description="x") for i in (1, 2)]
        with self.assertRaises(sc.ErrorConexionCasa) as ctx:
            sc.elegir_puerto(puertos)
        self.assertIn("CASA_PUERTO", str(ctx.exception))


class TestEstado(unittest.TestCase):

    def test_json_incompleto_o_raro(self):
        e = sc.EstadoCasa.desde_json({"t": "x", "leds": [1, 0], "track": None, "vol": "7"})
        self.assertIsNone(e.temperatura)
        self.assertEqual(e.leds, (True, False, False, False, False, False))
        self.assertEqual((e.pista, e.volumen), (0, 7))
        sc.EstadoCasa.desde_json({"leds": "roto"})


class TestMonitor(unittest.TestCase):

    def test_estado_y_cambios_de_conexion(self):
        casa = CasaSimulada()
        cliente = sc.ClienteSerie(CFG, fabrica_puerto=casa.abrir)
        self.addCleanup(cliente.cerrar)
        estados, conexiones = [], []
        monitor = sc.MonitorCasa(cliente, estados.append, lambda ok, msg: conexiones.append(ok), intervalo_s=0.02)

        monitor.sondear()
        monitor.sondear()
        self.assertEqual(len(estados), 2)
        self.assertEqual(conexiones, [True])           # solo avisa del cambio

        casa.desenchufar()
        self.assertTrue(esperar(lambda: not cliente.conectado))
        casa.silenciosa = True                         # al reenchufar no contesta aún
        monitor.sondear()
        self.assertEqual(conexiones, [True, False])
        monitor.sondear()
        self.assertEqual(conexiones, [True, False])    # sigue caída: no repite

        casa.silenciosa = False
        monitor.sondear()
        self.assertEqual(conexiones, [True, False, True])

    def test_hilo_del_monitor(self):
        casa = CasaSimulada()
        cliente = sc.ClienteSerie(CFG, fabrica_puerto=casa.abrir)
        self.addCleanup(cliente.cerrar)
        estados = []
        monitor = sc.MonitorCasa(cliente, estados.append, intervalo_s=0.02)
        monitor.start()
        self.assertTrue(esperar(lambda: len(estados) >= 3))
        monitor.parar()
        monitor.join(1)
        self.assertFalse(monitor.is_alive())


if __name__ == "__main__":
    unittest.main()
