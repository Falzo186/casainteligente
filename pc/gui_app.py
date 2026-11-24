"""
gui_app.py — Interfaz gráfica de la Casa Inteligente (PySide6).

    python gui_app.py            # con la placa conectada por USB
    python gui_app.py --demo     # con la placa simulada (sin hardware)

Estructura:
    Backend   (sin widgets) conecta la placa, el monitor, el asistente y la voz;
              todo lo que llega de otros hilos sale como señales de Qt.
    Ventana   solo dibuja y reenvía clics al Backend.

Hilos: el hilo de la interfaz nunca espera a la placa ni a la voz. Las órdenes
de los botones van a un hilo de trabajo; las frases (escritas o habladas) las
procesa el hilo del asistente; el monitor consulta STATUS cada segundo.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget,
)

import gui_widgets as gw
from acciones import Ejecutor
from catalogo import Catalogo, ErrorCatalogo
from config import DIALOGO, LOG, VOLUMEN_MAX, ZONAS_CASA
from dialogo import Asistente, FuenteCombinada, FuenteVoz
from entendedor import Entendedor
from serial_client import ClienteSerie, ErrorCasa, EstadoCasa, EventoCasa, MonitorCasa
from simulador_casa import CasaSimulada
from stt import ErrorMicrofono, ErrorSTT
from tts import Sintetizador

log = logging.getLogger("gui")


# ---------------------------------------------------------------------------
# Voz de respuesta
# ---------------------------------------------------------------------------

class VozGui:
    """
    Sintetizador que se crea en el hilo del asistente (en Windows el motor SAPI5
    debe usarse en el mismo hilo que lo creó) y que se puede silenciar.
    """

    def __init__(self) -> None:
        self.silenciado = False
        self._sintetizador: Sintetizador | None = None

    def hablar(self, texto: str) -> None:
        if self.silenciado:
            return
        if self._sintetizador is None:
            self._sintetizador = Sintetizador()
        self._sintetizador.hablar(texto)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class Backend(QObject):
    estado = Signal(object)            # EstadoCasa
    conexion = Signal(bool, str)
    evento_casa = Signal(object)       # EventoCasa
    mensaje = Signal(str, str)         # rol (usuario|asistente|alerta|sistema), texto
    tiempos = Signal(str)
    fase = Signal(str)                 # escuchando | pensando | hablando | cargando | inactivo

    def __init__(self, demo: bool = False) -> None:
        super().__init__()
        self.demo = demo
        try:
            self.catalogo = Catalogo.cargar()
            self._aviso_catalogo = None
        except ErrorCatalogo as e:
            self.catalogo = Catalogo([])
            self._aviso_catalogo = f"No se pudo cargar canciones.json: {e}"

        self.placa = CasaSimulada(ruido=True) if demo else None
        if self.placa is not None:                       # un poco de vida inicial en el modo demo
            self.placa.leds[3] = True
            self.placa.temperatura, self.placa.humedad = 24.5, 58.0
        self.cliente = ClienteSerie(fabrica_puerto=self.placa.abrir if self.placa else None)
        self.entendedor = Entendedor(self.catalogo)
        self.ejecutor = Ejecutor(self.cliente, self.catalogo)
        self.voz_respuesta = VozGui()
        self.fuente = FuenteCombinada()
        self.alertas: queue.Queue[str] = queue.Queue()
        self.asistente = Asistente(
            self.fuente, self.voz_respuesta, self.entendedor, self.ejecutor, self.alertas,
            al_evento=self._al_evento, al_fase=self._al_fase,
        )
        self.monitor = MonitorCasa(self.cliente, self._al_estado, self._al_conexion)
        self.ultimo_estado: EstadoCasa | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="acciones-gui")
        self._voz_activa = False
        self._voz_cargando = False

    # --- Arranque y cierre --------------------------------------------------

    def iniciar(self) -> None:
        self.cliente.suscribir(self._al_evento_casa)
        self.monitor.start()
        threading.Thread(target=self.asistente.ejecutar_bucle, daemon=True, name="asistente").start()
        threading.Thread(target=self._preparar_modelo, daemon=True, name="ollama").start()
        if self._aviso_catalogo:
            self.mensaje.emit("sistema", self._aviso_catalogo)
        self.fase.emit("inactivo")

    def _preparar_modelo(self) -> None:
        if not self.entendedor.verificar():
            self.mensaje.emit("sistema", "Ollama no está disponible: entenderé solo órdenes directas, sin el modelo de respaldo.")
            return
        self.entendedor.precargar()

    def cerrar(self) -> None:
        self.asistente.parar()
        self.monitor.parar()
        self._detener_voz()
        self.cliente.cerrar()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- Callbacks desde otros hilos ---------------------------------------

    def _al_estado(self, e: EstadoCasa) -> None:
        self.ultimo_estado = e
        self.estado.emit(e)

    def _al_conexion(self, ok: bool, texto: str) -> None:
        self.conexion.emit(ok, texto)
        if not ok:
            self.mensaje.emit("sistema", "Sin conexión con la placa. Revisa el cable USB; reconectaré sola.")
        elif self.monitor is not None and getattr(self, "_ya_conecto", False):
            self.mensaje.emit("sistema", "Placa reconectada.")
        self._ya_conecto = True if ok else getattr(self, "_ya_conecto", False)

    def _al_evento_casa(self, ev: EventoCasa) -> None:
        self.evento_casa.emit(ev)
        if ev.tipo == "intruso":
            self.alertas.put(DIALOGO.mensaje_alerta_intruso)     # el asistente lo dice en voz alta
        elif ev.tipo == "error_musica":
            self.alertas.put(DIALOGO.mensaje_error_musica)
        elif ev.tipo == "fin":
            self._pool.submit(self._refrescar)

    def _al_evento(self, tipo: str, texto: str, **meta) -> None:
        if tipo == "tiempos":
            self.tiempos.emit(texto)
        else:
            rol = {"oi": "usuario", "dije": "asistente", "alerta": "alerta", "sistema": "sistema"}.get(tipo, "sistema")
            self.mensaje.emit(rol, texto)
        if tipo == "dije":
            self._pool.submit(self._refrescar)                   # la orden ya se ejecutó: se actualiza el panel

    def _al_fase(self, fase: str) -> None:
        if fase == "escuchando" and not self._voz_activa:
            fase = "inactivo"
        self.fase.emit(fase)

    def _refrescar(self) -> None:
        try:
            self.monitor.sondear()
        except Exception:
            log.exception("No se pudo refrescar el estado")

    # --- Acciones de la interfaz ---------------------------------------------

    def _lanzar(self, trabajo: Callable[[], None]) -> None:
        """Ejecuta una orden a la placa fuera del hilo de la interfaz y refresca el panel."""
        def envoltorio() -> None:
            try:
                trabajo()
            except ErrorCasa as e:
                self.mensaje.emit("sistema", f"No se pudo: {e}")
            except Exception:
                log.exception("Error en una acción de la interfaz")
            self._refrescar()
        self._pool.submit(envoltorio)

    def alternar_zona(self, zona: str) -> None:
        numero = ZONAS_CASA[zona][0]
        actual = self.ultimo_estado.leds[numero - 1] if self.ultimo_estado else False
        self._lanzar(lambda: self.cliente.set_led(numero, not actual))

    def alternar_ventilador(self) -> None:
        actual = self.ultimo_estado.ventilador if self.ultimo_estado else False
        self._lanzar(lambda: self.cliente.set_ventilador(not actual))

    def alternar_guardian(self) -> None:
        e = self.ultimo_estado
        if e is not None and e.alarma:
            self._lanzar(self.cliente.apagar_alarma)
        else:
            activo = e.guardian if e else False
            self._lanzar(lambda: self.cliente.set_guardian(not activo))

    def apagar_alarma(self) -> None:
        self._lanzar(self.cliente.apagar_alarma)

    def reproducir(self, pista: int) -> None:
        self._lanzar(lambda: self.cliente.reproducir(pista))

    def detener_musica(self) -> None:
        self._lanzar(self.cliente.detener)

    def fijar_volumen(self, valor: int) -> None:
        self._lanzar(lambda: self.cliente.set_volumen(valor))

    def enviar_texto(self, texto: str) -> None:
        self.fuente.enviar_texto(texto)

    def simular_intruso(self) -> None:
        """Solo en el modo demo: el PIR de la placa simulada detecta movimiento."""
        if self.placa is None:
            return
        if not (self.ultimo_estado and self.ultimo_estado.guardian):
            self.mensaje.emit("sistema", "Activa primero el modo guardián para probar la alarma.")
            return
        self.placa.disparar_intruso()

    def alternar_silencio(self) -> bool:
        self.voz_respuesta.silenciado = not self.voz_respuesta.silenciado
        return self.voz_respuesta.silenciado

    # --- Micrófono ------------------------------------------------------------

    def alternar_voz(self) -> None:
        if self._voz_cargando:
            return
        if self._voz_activa:
            self._detener_voz()
            self.fase.emit("inactivo")
        else:
            threading.Thread(target=self._activar_voz, daemon=True, name="activar-voz").start()

    def _activar_voz(self) -> None:
        self._voz_cargando = True
        self.fase.emit("cargando")
        try:
            voz = FuenteVoz()
            voz.iniciar()          # carga Whisper (la primera vez lo descarga) y abre el micrófono
        except (ErrorMicrofono, ErrorSTT) as e:
            self.mensaje.emit("sistema", f"No se pudo activar el micrófono: {e}")
            self.fase.emit("inactivo")
        except Exception as e:
            log.exception("Error al activar la voz")
            self.mensaje.emit("sistema", f"No se pudo activar el micrófono: {e}")
            self.fase.emit("inactivo")
        else:
            self.fuente.voz = voz
            self._voz_activa = True
            self.mensaje.emit("sistema", "Micrófono activado: te escucho.")
            self.fase.emit("escuchando")
        finally:
            self._voz_cargando = False

    def _detener_voz(self) -> None:
        voz, self.fuente.voz = self.fuente.voz, None
        self._voz_activa = False
        if voz is not None:
            time.sleep(0.4)        # deja terminar la escucha en curso antes de cerrar el micrófono
            voz.detener()


# ---------------------------------------------------------------------------
# Ventana
# ---------------------------------------------------------------------------

class Ventana(QMainWindow):

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend
        self.setWindowTitle("Casa Inteligente")
        self.resize(1340, 860)
        self.setMinimumSize(1180, 760)

        raiz = QWidget()
        raiz.setObjectName("raiz")
        self.setCentralWidget(raiz)
        principal = QVBoxLayout(raiz)
        principal.setContentsMargins(24, 18, 24, 22)
        principal.setSpacing(14)

        principal.addLayout(self._cabecera())
        self.banner = self._banner_alarma()
        principal.addWidget(self.banner)

        cuerpo = QHBoxLayout()
        cuerpo.setSpacing(16)
        cuerpo.addLayout(self._columna_casa(), 11)
        cuerpo.addLayout(self._columna_asistente(), 9)
        principal.addLayout(cuerpo, 1)

        self._conectar_senales()
        self.chat.set_fase("inactivo")
        self.chat.agregar("sistema", "Hola, soy tu casa inteligente. Prueba: «prende la luz de la sala», «pon música» o «qué temperatura hace».")

    # --- Construcción -----------------------------------------------------------

    def _cabecera(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        fila.setSpacing(14)
        titulos = QVBoxLayout()
        titulos.setSpacing(0)
        nombre = QLabel("Casa Inteligente")
        nombre.setStyleSheet("font-size: 26px; font-weight: 800; letter-spacing: 0.5px;")
        sub = QLabel("Panel de control y asistente de voz")
        sub.setStyleSheet(f"color: {gw.TEXTO2}; font-size: 12px;")
        titulos.addWidget(nombre)
        titulos.addWidget(sub)
        fila.addLayout(titulos)
        fila.addStretch(1)

        if self.backend.demo:
            demo = QPushButton("🧪  Simular intruso")
            demo.setObjectName("fantasma")
            demo.clicked.connect(self.backend.simular_intruso)
            fila.addWidget(demo)

        self.silencio = QPushButton("🔊")
        self.silencio.setObjectName("secundario")
        self.silencio.setToolTip("Silenciar o activar la voz de las respuestas")
        self.silencio.setFixedSize(46, 46)
        self.silencio.clicked.connect(self._alternar_silencio)
        fila.addWidget(self.silencio)

        self.pildora = gw.PildoraEstado()
        self.pildora.set_estado("Conectando…", gw.AMBAR)
        fila.addWidget(self.pildora)
        self.guardian = gw.BotonGuardian()
        fila.addWidget(self.guardian)
        return fila

    def _banner_alarma(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("banner")
        banner.setStyleSheet(   # por nombre: un selector "QFrame" también alcanzaría a las QLabel (son QFrame)
            f"#banner {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #5A0F22, stop:1 #8A1A35);"
            f" border: 1px solid {gw.ROJO}; border-radius: 16px; }}")
        fila = QHBoxLayout(banner)
        fila.setContentsMargins(20, 10, 14, 10)
        texto = QLabel("🚨  ¡INTRUSO DETECTADO!   La alarma está sonando")
        texto.setStyleSheet("font-size: 15px; font-weight: 800; color: white;")
        boton = QPushButton("Apagar alarma")
        boton.setObjectName("peligro")
        boton.setStyleSheet("QPushButton { background: white; color: #8A1A35; border-radius: 12px; padding: 8px 18px; font-weight: 800; }"
                            "QPushButton:hover { background: #FFE3E8; }")
        boton.clicked.connect(self.backend.apagar_alarma)
        fila.addWidget(texto, 1)
        fila.addWidget(boton)
        banner.hide()
        return banner

    def _columna_casa(self) -> QVBoxLayout:
        columna = QVBoxLayout()
        columna.setSpacing(16)
        plano = gw.Tarjeta("Plano de la casa")
        self.plano = gw.PlanoCasa()
        plano.cuerpo.addWidget(self.plano, 1)
        pista = QLabel("Toca una habitación para encender o apagar su luz")
        pista.setStyleSheet(f"color: {gw.TEXTO2}; font-size: 11px;")
        plano.cuerpo.addWidget(pista)
        columna.addWidget(plano, 1)

        sensores = QHBoxLayout()
        sensores.setSpacing(16)
        self.temperatura = gw.ArcoMedidor("°C", 0, 50, gw.color_temperatura, 1)
        sensores.addWidget(self._tarjeta_sensor("Temperatura", self.temperatura))
        self.humedad = gw.ArcoMedidor("% humedad", 0, 100, gw.color_humedad, 0)
        sensores.addWidget(self._tarjeta_sensor("Humedad", self.humedad))
        self.movimiento = gw.IndicadorMovimiento()
        self.etiqueta_movimiento = QLabel("Sin movimiento")
        sensores.addWidget(self._tarjeta_sensor("Movimiento", self.movimiento, self.etiqueta_movimiento))
        self.ventilador = gw.Ventilador()
        self.etiqueta_ventilador = QLabel("Apagado")
        sensores.addWidget(self._tarjeta_sensor("Ventilador", self.ventilador, self.etiqueta_ventilador))
        columna.addLayout(sensores)
        return columna

    @staticmethod
    def _tarjeta_sensor(titulo: str, widget: QWidget, pie: QLabel | None = None) -> gw.Tarjeta:
        tarjeta = gw.Tarjeta(titulo, margenes=14)
        tarjeta.cuerpo.addWidget(widget, 1)
        if pie is not None:
            pie.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pie.setStyleSheet(f"color: {gw.TEXTO2}; font-size: 12px;")
            tarjeta.cuerpo.addWidget(pie)
        return tarjeta

    def _columna_asistente(self) -> QVBoxLayout:
        columna = QVBoxLayout()
        columna.setSpacing(16)
        tarjeta_chat = gw.Tarjeta("Asistente de IA")
        self.chat = gw.PanelChat()
        tarjeta_chat.cuerpo.addWidget(self.chat, 1)
        columna.addWidget(tarjeta_chat, 6)

        tarjeta_musica = gw.Tarjeta("Música")
        canciones = [(c.pista, c.titulo) for c in self.backend.catalogo.canciones]
        self.musica = gw.PanelMusica(canciones)
        tarjeta_musica.cuerpo.addWidget(self.musica, 1)
        columna.addWidget(tarjeta_musica, 4)
        return columna

    def _conectar_senales(self) -> None:
        b = self.backend
        b.estado.connect(self._aplicar_estado)
        b.conexion.connect(self._aplicar_conexion)
        b.mensaje.connect(self.chat.agregar)
        b.tiempos.connect(self.chat.poner_tiempos)
        b.fase.connect(self.chat.set_fase)
        self.plano.zona_pulsada.connect(b.alternar_zona)
        self.ventilador.pulsado.connect(b.alternar_ventilador)
        self.guardian.clicked.connect(b.alternar_guardian)
        self.chat.texto_enviado.connect(b.enviar_texto)
        self.chat.microfono_pulsado.connect(b.alternar_voz)
        self.musica.reproducir.connect(b.reproducir)
        self.musica.detener.connect(b.detener_musica)
        self.musica.volumen.connect(b.fijar_volumen)

    # --- Reacciones ------------------------------------------------------------

    def _aplicar_estado(self, e: EstadoCasa) -> None:
        self.plano.set_estado(e.leds, e.alarma)
        self.temperatura.set_valor(e.temperatura)
        self.humedad.set_valor(e.humedad)
        self.movimiento.set_activo(e.movimiento)
        self.etiqueta_movimiento.setText("¡Movimiento!" if e.movimiento else "Sin movimiento")
        self.ventilador.set_encendido(e.ventilador)
        self.etiqueta_ventilador.setText("Encendido" if e.ventilador else "Apagado")
        self.musica.set_estado(e.pista, e.volumen, e.mp3, e.alarma)
        self.guardian.set_estado("alarma" if e.alarma else "armado" if e.guardian else "apagado")
        self.banner.setVisible(e.alarma)

    def _aplicar_conexion(self, ok: bool, texto: str) -> None:
        if ok:
            self.pildora.set_estado("Modo demo" if self.backend.demo else texto, gw.VIOLETA if self.backend.demo else gw.VERDE)
        else:
            self.pildora.set_estado("Sin conexión con la placa", gw.ROJO)

    def _alternar_silencio(self) -> None:
        silenciado = self.backend.alternar_silencio()
        self.silencio.setText("🔇" if silenciado else "🔊")

    def closeEvent(self, e) -> None:
        self.backend.cerrar()
        super().closeEvent(e)


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------

def crear_aplicacion() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont(gw.FUENTE, 10))
    app.setStyleSheet(gw.ESTILO)
    return app


def main(argv: list[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(description="Casa Inteligente - interfaz gráfica")
    analizador.add_argument("--demo", action="store_true", help="usar la placa simulada (sin hardware)")
    analizador.add_argument("--debug", action="store_true", help="registro detallado")
    args = analizador.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else getattr(logging, LOG.nivel.upper(), logging.INFO),
                        format=LOG.formato, datefmt=LOG.formato_fecha)
    for ruidosa in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(ruidosa).setLevel(logging.WARNING)

    app = crear_aplicacion()
    backend = Backend(demo=args.demo)
    ventana = Ventana(backend)
    ventana.show()
    backend.iniciar()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
