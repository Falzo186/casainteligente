"""
Prueba de humo de la interfaz gráfica: se abre sin pantalla (plataforma
"offscreen") con la placa simulada y se comprueba que los clics, el chat y la
alarma llegan de punta a punta.

    python -m unittest tests.test_gui -v
"""

import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))

try:
    from PySide6.QtWidgets import QApplication
    import gui_app
except ImportError as e:      # PySide6 no instalado
    raise unittest.SkipTest(f"falta una dependencia: {e}")


def esperar(app, condicion, segundos=4.0):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        app.processEvents()
        if condicion():
            return True
        time.sleep(0.01)
    app.processEvents()
    return condicion()


class TestInterfaz(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = gui_app.crear_aplicacion()

    def setUp(self):
        self.backend = gui_app.Backend(demo=True)
        self.backend.voz_respuesta.silenciado = True      # sin sonido en las pruebas
        self.ventana = gui_app.Ventana(self.backend)
        self.ventana.show()
        self.backend.iniciar()
        self.placa = self.backend.placa
        self.addCleanup(self.backend.cerrar)
        self.addCleanup(self.ventana.close)
        self.assertTrue(esperar(self.app, lambda: self.backend.ultimo_estado is not None), "no llegó el estado")

    def test_conexion_y_estado_inicial(self):
        self.assertTrue(esperar(self.app, lambda: self.ventana.pildora._texto == "Modo demo"))
        self.assertEqual(self.ventana.plano._objetivo["sala"], 1.0)       # el demo arranca con la sala encendida
        self.assertEqual(self.ventana.plano._objetivo["cocina"], 0.0)
        self.assertAlmostEqual(self.ventana.temperatura._valor, 24.5, delta=1.0)   # el demo varía un poco

    def test_clic_en_una_habitacion(self):
        self.ventana.plano.zona_pulsada.emit("cocina")
        self.assertTrue(esperar(self.app, lambda: self.placa.leds[1]))
        self.assertTrue(esperar(self.app, lambda: self.ventana.plano._objetivo["cocina"] == 1.0))
        self.ventana.plano.zona_pulsada.emit("cocina")                    # segundo clic: apaga
        self.assertTrue(esperar(self.app, lambda: not self.placa.leds[1]))

    def test_ventilador_y_musica_desde_los_controles(self):
        self.ventana.ventilador.pulsado.emit()
        self.assertTrue(esperar(self.app, lambda: self.placa.ventilador))
        self.ventana.musica.reproducir.emit(4)
        self.assertTrue(esperar(self.app, lambda: self.placa.pista == 4))
        self.assertTrue(esperar(self.app, lambda: "Canción tres" in self.ventana.musica.sonando.text()))
        self.ventana.musica.detener.emit()
        self.assertTrue(esperar(self.app, lambda: self.placa.pista == 0))

    def test_chat_ejecuta_ordenes_y_pregunta(self):
        self.ventana.chat.texto_enviado.emit("prende las luces")
        textos = lambda: [b.etiqueta.text() for b in self.ventana.chat._burbujas]   # noqa: E731
        self.assertTrue(esperar(self.app, lambda: any("¿Qué luces quieres encender" in t for t in textos())))
        self.ventana.chat.texto_enviado.emit("en la cocina")
        self.assertTrue(esperar(self.app, lambda: self.placa.leds[1]))
        self.assertTrue(esperar(self.app, lambda: "Luces de la cocina encendidas." in textos()))
        roles = [b.rol for b in self.ventana.chat._burbujas]
        self.assertIn("usuario", roles)
        self.assertIn("asistente", roles)

    def test_alarma_de_punta_a_punta(self):
        self.ventana.guardian.clicked.emit()                              # activa el guardián
        self.assertTrue(esperar(self.app, lambda: self.placa.guardian))
        self.assertTrue(esperar(self.app, lambda: self.ventana.guardian._estado == "armado"))
        self.assertTrue(self.ventana.banner.isHidden())

        self.backend.simular_intruso()
        self.assertTrue(esperar(self.app, lambda: not self.ventana.banner.isHidden()))
        self.assertEqual(self.ventana.guardian._estado, "alarma")
        # El asistente anuncia la alerta en el chat.
        self.assertTrue(esperar(self.app, lambda: any(b.rol == "alerta" for b in self.ventana.chat._burbujas)))

        self.ventana.guardian.clicked.emit()                              # con alarma, el botón la apaga
        self.assertTrue(esperar(self.app, lambda: not self.placa.alarma))
        self.assertTrue(esperar(self.app, lambda: self.ventana.banner.isHidden()))

    def test_intruso_sin_guardian_avisa(self):
        self.backend.simular_intruso()
        self.assertTrue(esperar(self.app, lambda: any(
            "modo guardián" in b.etiqueta.text() for b in self.ventana.chat._burbujas)))

    def test_desconexion_de_la_placa(self):
        # El simulador "se reenchufa" solo al reconectar: se deja mudo mientras está desenchufado.
        self.placa.silenciosa = True
        self.placa.desenchufar()
        self.assertTrue(esperar(self.app, lambda: self.ventana.pildora._texto == "Sin conexión con la placa", 15.0))
        self.assertTrue(any("Sin conexión" in b.etiqueta.text() for b in self.ventana.chat._burbujas))
        self.placa.silenciosa = False                                      # vuelve a responder
        self.assertTrue(esperar(self.app, lambda: self.ventana.pildora._texto == "Modo demo", 15.0))
        self.assertTrue(any("reconectada" in b.etiqueta.text() for b in self.ventana.chat._burbujas))


if __name__ == "__main__":
    unittest.main()
