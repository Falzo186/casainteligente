"""
gui_widgets.py — Componentes visuales de la interfaz (PySide6).

Todo se dibuja por código con QPainter (sin imágenes externas), así la
interfaz escala bien y no hay archivos que se pierdan:

    PlanoCasa        plano de la casa; cada habitación brilla al encender su foco
    ArcoMedidor      medidor de arco animado (temperatura, humedad)
    Ventilador       aspas que giran cuando está encendido
    IndicadorMovimiento  ondas cuando el PIR detecta algo
    OrbeVoz          botón de micrófono con animaciones por fase
    BotonGuardian    botón del modo guardián (apagado / armado / alarma)
    PanelChat        conversación con el asistente + entrada de texto + voz
    PanelMusica      lista de canciones, ecualizador y volumen
    Tarjeta          contenedor con esquinas redondeadas
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from config import VOLUMEN_MAX, ZONAS_CASA

# ---------------------------------------------------------------------------
# Paleta y estilo global
# ---------------------------------------------------------------------------

FONDO = "#080C18"
PANEL = "#111729"
PANEL2 = "#18203A"
PANEL3 = "#1F2947"
BORDE = "#2A3558"
TEXTO = "#E9EDFA"
TEXTO2 = "#8D98BC"
CIAN = "#4FD1FF"
VIOLETA = "#8B7BFF"
AMBAR = "#FFC857"
NARANJA = "#FF9F43"
VERDE = "#3DDC97"
ROJO = "#FF4D6D"

FUENTE = "Segoe UI"

ESTILO = f"""
QWidget {{ color: {TEXTO}; font-family: '{FUENTE}', 'Inter', 'Helvetica Neue', sans-serif; font-size: 13px; }}
QMainWindow, QWidget#raiz {{ background: {FONDO}; }}
QLabel {{ background: transparent; }}
QFrame#tarjeta {{ background: {PANEL}; border: 1px solid {BORDE}; border-radius: 20px; }}
QLabel#titulo_tarjeta {{ color: {TEXTO2}; font-size: 11px; font-weight: 600; letter-spacing: 2px; }}
QLineEdit {{ background: {PANEL2}; border: 1px solid {BORDE}; border-radius: 16px; padding: 10px 16px;
            selection-background-color: {VIOLETA}; font-size: 14px; }}
QLineEdit:focus {{ border: 1px solid {CIAN}; }}
QPushButton#chip {{ background: {PANEL2}; border: 1px solid {BORDE}; border-radius: 14px; padding: 6px 12px;
                   color: {TEXTO2}; font-size: 12px; }}
QPushButton#chip:hover {{ border: 1px solid {CIAN}; color: {TEXTO}; background: {PANEL3}; }}
QPushButton#enviar {{ background: {CIAN}; border: none; border-radius: 18px; color: #04101C; font-weight: 700;
                     font-size: 16px; min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; }}
QPushButton#enviar:hover {{ background: #7BE0FF; }}
QPushButton#secundario {{ background: {PANEL2}; border: 1px solid {BORDE}; border-radius: 14px; padding: 8px 16px;
                         color: {TEXTO}; font-weight: 600; }}
QPushButton#secundario:hover {{ border: 1px solid {CIAN}; }}
QPushButton#peligro {{ background: {ROJO}; border: none; border-radius: 14px; padding: 8px 18px; color: white;
                      font-weight: 700; }}
QPushButton#peligro:hover {{ background: #FF7088; }}
QPushButton#fantasma {{ background: transparent; border: 1px dashed {BORDE}; border-radius: 12px; padding: 5px 12px;
                       color: {TEXTO2}; font-size: 11px; }}
QPushButton#fantasma:hover {{ color: {AMBAR}; border-color: {AMBAR}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDE}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3A4670; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QListWidget {{ background: transparent; border: none; outline: 0; font-size: 13px; }}
QListWidget::item {{ padding: 9px 12px; border-radius: 12px; margin: 1px 0; color: {TEXTO}; }}
QListWidget::item:hover {{ background: {PANEL2}; }}
QListWidget::item:selected {{ background: {PANEL3}; color: {AMBAR}; }}
QSlider::groove:horizontal {{ height: 6px; background: {PANEL3}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {CIAN}, stop:1 {VIOLETA});
                               border-radius: 3px; }}
QSlider::handle:horizontal {{ background: white; width: 16px; height: 16px; margin: -5px 0; border-radius: 8px; }}
QToolTip {{ background: {PANEL3}; color: {TEXTO}; border: 1px solid {BORDE}; padding: 4px 8px; }}
"""


# ---------------------------------------------------------------------------
# Utilidades de dibujo
# ---------------------------------------------------------------------------

def color(hex_: str, alfa: float = 1.0) -> QColor:
    c = QColor(hex_)
    c.setAlphaF(max(0.0, min(1.0, alfa)))
    return c


def mezclar(a: str, b: str, t: float) -> QColor:
    """Color intermedio entre a y b (t=0 -> a, t=1 -> b)."""
    ca, cb = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


def acercar(actual: float, objetivo: float, velocidad: float) -> float:
    """Mueve `actual` hacia `objetivo` (suavizado exponencial)."""
    return objetivo if abs(objetivo - actual) < 0.004 else actual + (objetivo - actual) * velocidad


def _fuente(puntos: float, peso: int = 400) -> QFont:
    f = QFont(FUENTE)
    f.setPointSizeF(puntos)
    f.setWeight(QFont.Weight(peso))
    return f


def _suavizado(p: QPainter) -> None:
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)


class Reloj(QTimer):
    """Temporizador de animación que solo corre mientras se necesita."""

    def __init__(self, parent: QWidget, paso, intervalo_ms: int = 16) -> None:
        super().__init__(parent)
        self.setInterval(intervalo_ms)
        self.timeout.connect(paso)


# ---------------------------------------------------------------------------
# Tarjeta
# ---------------------------------------------------------------------------

class Tarjeta(QFrame):
    """Contenedor redondeado con un título pequeño en mayúsculas."""

    def __init__(self, titulo: str = "", parent: QWidget | None = None, margenes: int = 18) -> None:
        super().__init__(parent)
        self.setObjectName("tarjeta")
        self.cuerpo = QVBoxLayout(self)
        self.cuerpo.setContentsMargins(margenes, margenes - 2, margenes, margenes)
        self.cuerpo.setSpacing(10)
        self.titulo = QLabel(titulo.upper())
        self.titulo.setObjectName("titulo_tarjeta")
        self.cabecera = QHBoxLayout()
        self.cabecera.addWidget(self.titulo)
        self.cabecera.addStretch(1)
        if titulo:
            self.cuerpo.addLayout(self.cabecera)


# ---------------------------------------------------------------------------
# Plano de la casa
# ---------------------------------------------------------------------------

# Posición de cada habitación en fracciones del widget: (x, y, ancho, alto)
HABITACIONES = {
    "bano": (0.03, 0.03, 0.22, 0.38),
    "cocina": (0.25, 0.03, 0.32, 0.38),
    "comedor": (0.57, 0.03, 0.40, 0.38),
    "sala": (0.03, 0.41, 0.46, 0.34),
    "cuarto": (0.49, 0.41, 0.48, 0.34),
    "exterior": (0.03, 0.75, 0.94, 0.22),
}
NOMBRE_HABITACION = {
    "bano": "Baño", "cocina": "Cocina", "comedor": "Comedor", "sala": "Sala",
    "cuarto": "Cuarto", "exterior": "Exterior · Entrada",
}


class PlanoCasa(QWidget):
    zona_pulsada = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 300)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._objetivo = {z: 0.0 for z in ZONAS_CASA}
        self._nivel = {z: 0.0 for z in ZONAS_CASA}
        self._sobre: str | None = None
        self._alarma = False
        self._fase = 0.0
        self._reloj = Reloj(self, self._animar)
        self._reloj.start()

    def set_estado(self, leds: tuple[bool, ...], alarma: bool) -> None:
        for (zona, _), encendido in zip(ZONAS_CASA.items(), leds):
            self._objetivo[zona] = 1.0 if encendido else 0.0
        self._alarma = alarma

    def _animar(self) -> None:
        self._fase = (self._fase + 0.06) % (2 * math.pi)
        for z in self._nivel:
            self._nivel[z] = acercar(self._nivel[z], self._objetivo[z], 0.16)
        self.update()

    def _rect(self, zona: str) -> QRectF:
        fx, fy, fw, fh = HABITACIONES[zona]
        return QRectF(fx * self.width(), fy * self.height(), fw * self.width(), fh * self.height()).adjusted(4, 4, -4, -4)

    def _zona_en(self, punto: QPointF) -> str | None:
        return next((z for z in HABITACIONES if self._rect(z).contains(punto)), None)

    def mouseMoveEvent(self, e) -> None:
        zona = self._zona_en(e.position())
        if zona != self._sobre:
            self._sobre = zona
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor if zona else Qt.CursorShape.ArrowCursor))

    def leaveEvent(self, e) -> None:
        self._sobre = None

    def mousePressEvent(self, e) -> None:
        zona = self._zona_en(e.position())
        if zona and e.button() == Qt.MouseButton.LeftButton:
            self.zona_pulsada.emit(zona)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        pulso = 0.5 + 0.5 * math.sin(self._fase * 3)
        for zona in HABITACIONES:
            self._dibujar_habitacion(p, zona, pulso)

    def _dibujar_habitacion(self, p: QPainter, zona: str, pulso: float) -> None:
        r = self._rect(zona)
        n = self._nivel[zona]
        radio = 18.0

        # Resplandor suave alrededor de la habitación encendida.
        if n > 0.02:
            for i in range(7, 0, -1):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color(AMBAR, 0.035 * n * (8 - i) / 7))
                p.drawRoundedRect(r.adjusted(-i * 2.5, -i * 2.5, i * 2.5, i * 2.5), radio + i * 2, radio + i * 2)

        ruta = QPainterPath()
        ruta.addRoundedRect(r, radio, radio)
        p.fillPath(ruta, QBrush(mezclar(PANEL2, "#12182B", 0.0)))

        if n > 0.01:
            p.save()
            p.setOpacity(n)
            degradado = QRadialGradient(r.center().x(), r.top() + r.height() * 0.15, max(r.width(), r.height()) * 0.95)
            degradado.setColorAt(0.0, QColor("#FFE29A"))
            degradado.setColorAt(0.45, QColor(AMBAR))
            degradado.setColorAt(1.0, QColor("#E58A2E"))
            p.fillPath(ruta, QBrush(degradado))
            p.restore()

        # Borde: rojo parpadeante en alarma; cian al pasar el ratón.
        if self._alarma:
            borde = color(ROJO, 0.35 + 0.65 * pulso)
            ancho = 2.5
        elif self._sobre == zona:
            borde, ancho = color(CIAN), 2.0
        else:
            borde, ancho = mezclar(BORDE, AMBAR, n), 1.4
        p.setPen(QPen(borde, ancho))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, radio, radio)

        # Texto: oscuro sobre fondo encendido, claro sobre apagado.
        tinta = mezclar(TEXTO, "#2A1B00", n)
        tinta_suave = mezclar(TEXTO2, "#5A3C08", n)
        estado = "Encendida" if self._objetivo[zona] > 0.5 else "Apagada"
        izquierda = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        baja = r.height() < 78            # franja baja: nombre y estado en una sola línea
        p.setPen(tinta)
        p.setFont(_fuente(12.5, 650))
        if baja:
            p.drawText(QRectF(r.left() + 16, r.top(), r.width() * 0.5, r.height()), izquierda, NOMBRE_HABITACION[zona])
            p.setPen(tinta_suave)
            p.setFont(_fuente(9.5, 500))
            p.drawText(QRectF(r.left() + r.width() * 0.5, r.top(), r.width() * 0.5 - 56, r.height()),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, estado)
            self._dibujar_foco(p, QPointF(r.right() - 26, r.center().y() - 3), n, tinta_suave)
            return
        p.drawText(QRectF(r.left() + 16, r.top() + 12, r.width() - 60, 26), izquierda, NOMBRE_HABITACION[zona])
        p.setPen(tinta_suave)
        p.setFont(_fuente(9.5, 500))
        p.drawText(QRectF(r.left() + 16, r.top() + 36, r.width() - 60, 20), izquierda, estado)
        self._dibujar_foco(p, QPointF(r.right() - 26, r.top() + 28), n, tinta_suave)

    @staticmethod
    def _dibujar_foco(p: QPainter, c: QPointF, n: float, tinta: QColor) -> None:
        p.setPen(QPen(mezclar(TEXTO2, "#5A3C08", n), 1.8))
        p.setBrush(mezclar(PANEL3, "#FFF3C4", n))
        p.drawEllipse(c, 9, 9)
        p.drawLine(QPointF(c.x() - 4, c.y() + 12), QPointF(c.x() + 4, c.y() + 12))
        p.drawLine(QPointF(c.x() - 3, c.y() + 15), QPointF(c.x() + 3, c.y() + 15))
        if n > 0.3:
            p.setPen(QPen(color("#5A3C08", n), 1.4))
            for ang in (-60, -30, 0, 30, 60):
                a = math.radians(ang - 90)
                p.drawLine(QPointF(c.x() + math.cos(a) * 13, c.y() + math.sin(a) * 13),
                           QPointF(c.x() + math.cos(a) * 17, c.y() + math.sin(a) * 17))


# ---------------------------------------------------------------------------
# Medidor de arco
# ---------------------------------------------------------------------------

class ArcoMedidor(QWidget):
    """Medidor de 270°. `colores` es una función valor -> color para el arco."""

    def __init__(self, unidad: str, minimo: float, maximo: float, colores, decimales: int = 1,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(130, 130)
        self._unidad, self._min, self._max = unidad, minimo, maximo
        self._colores, self._decimales = colores, decimales
        self._valor: float | None = None
        self._mostrado = minimo
        self._reloj = Reloj(self, self._animar)
        self._reloj.start()

    def set_valor(self, valor: float | None) -> None:
        self._valor = valor

    def _animar(self) -> None:
        if self._valor is not None and abs(self._mostrado - self._valor) > 0.005:
            self._mostrado += (self._valor - self._mostrado) * 0.12
            self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        lado = min(self.width(), self.height()) - 16
        r = QRectF((self.width() - lado) / 2, (self.height() - lado) / 2, lado, lado)
        grosor = max(9.0, lado * 0.085)
        arco = r.adjusted(grosor / 2, grosor / 2, -grosor / 2, -grosor / 2)

        pista = QPen(color(PANEL3), grosor, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pista)
        p.drawArc(arco, 225 * 16, -270 * 16)

        if self._valor is not None:
            fraccion = max(0.0, min(1.0, (self._mostrado - self._min) / (self._max - self._min)))
            if fraccion > 0.003:
                p.setPen(QPen(self._colores(self._mostrado), grosor, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(arco, 225 * 16, int(-270 * 16 * fraccion))
            texto = f"{self._mostrado:.{self._decimales}f}"
        else:
            texto = "--"
        p.setPen(QColor(TEXTO))
        p.setFont(_fuente(max(15.0, lado * 0.17), 700))
        p.drawText(QRectF(r.left(), r.center().y() - lado * 0.16, r.width(), lado * 0.26), Qt.AlignmentFlag.AlignCenter, texto)
        p.setPen(QColor(TEXTO2))
        p.setFont(_fuente(max(9.0, lado * 0.075), 500))
        p.drawText(QRectF(r.left(), r.center().y() + lado * 0.08, r.width(), lado * 0.14), Qt.AlignmentFlag.AlignCenter, self._unidad)


def color_temperatura(t: float) -> QColor:
    """Azul frío -> cian -> ámbar -> rojo."""
    if t < 18:
        return mezclar("#4A8BFF", CIAN, (t - 5) / 13)
    if t < 26:
        return mezclar(CIAN, VERDE, (t - 18) / 8)
    if t < 32:
        return mezclar(VERDE, AMBAR, (t - 26) / 6)
    return mezclar(AMBAR, ROJO, (t - 32) / 8)


def color_humedad(h: float) -> QColor:
    return mezclar("#6FA8FF", VIOLETA, h / 100)


# ---------------------------------------------------------------------------
# Ventilador e indicador de movimiento
# ---------------------------------------------------------------------------

class Ventilador(QWidget):
    pulsado = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(130, 130)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._encendido = False
        self._velocidad = 0.0      # grados por fotograma
        self._angulo = 0.0
        self._reloj = Reloj(self, self._animar)
        self._reloj.start()

    def set_encendido(self, encendido: bool) -> None:
        self._encendido = encendido

    def _animar(self) -> None:
        self._velocidad = acercar(self._velocidad, 11.0 if self._encendido else 0.0, 0.05)
        if self._velocidad > 0.05:
            self._angulo = (self._angulo + self._velocidad) % 360
            self.update()
        elif self._velocidad != 0.0:
            self._velocidad = 0.0
            self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.pulsado.emit()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        lado = min(self.width(), self.height()) - 16
        centro = QPointF(self.width() / 2, self.height() / 2)
        radio = lado / 2
        activo = self._encendido
        tinta = QColor(CIAN) if activo else QColor("#4A5578")

        if activo:
            halo = QRadialGradient(centro, radio * 1.1)
            halo.setColorAt(0.5, color(CIAN, 0.0))
            halo.setColorAt(1.0, color(CIAN, 0.18))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(centro, radio * 1.1, radio * 1.1)

        p.setPen(QPen(tinta, 3))
        p.setBrush(color(PANEL2))
        p.drawEllipse(centro, radio * 0.92, radio * 0.92)

        p.save()
        p.translate(centro)
        p.rotate(self._angulo)
        for i in range(3):
            p.save()
            p.rotate(i * 120)
            aspa = QPainterPath()
            aspa.moveTo(0, 0)
            aspa.cubicTo(radio * 0.10, -radio * 0.30, radio * 0.45, -radio * 0.78, radio * 0.10, -radio * 0.80)
            aspa.cubicTo(-radio * 0.30, -radio * 0.78, -radio * 0.28, -radio * 0.25, 0, 0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color(tinta.name(), 0.9))
            p.drawPath(aspa)
            p.restore()
        p.restore()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(FONDO))
        p.drawEllipse(centro, radio * 0.11, radio * 0.11)


class IndicadorMovimiento(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(130, 130)
        self._activo = False
        self._fase = 0.0
        self._reloj = Reloj(self, self._animar, 33)
        self._reloj.start()

    def set_activo(self, activo: bool) -> None:
        self._activo = activo

    def _animar(self) -> None:
        self._fase = (self._fase + 0.035) % 1.0
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        centro = QPointF(self.width() / 2, self.height() / 2)
        base = min(self.width(), self.height()) / 2 - 8
        tinta = QColor(AMBAR) if self._activo else QColor("#3A4670")
        if self._activo:
            for k in range(3):
                f = (self._fase + k / 3) % 1.0
                p.setPen(QPen(color(AMBAR, 0.55 * (1 - f)), 2.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(centro, base * (0.35 + 0.65 * f), base * (0.35 + 0.65 * f))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tinta)
        p.drawEllipse(centro, base * 0.30, base * 0.30)
        # Silueta de persona
        p.setBrush(color(FONDO, 0.85))
        p.drawEllipse(QPointF(centro.x(), centro.y() - base * 0.10), base * 0.07, base * 0.07)
        cuerpo = QPainterPath()
        cuerpo.addRoundedRect(QRectF(centro.x() - base * 0.10, centro.y() - base * 0.01, base * 0.20, base * 0.17), 5, 5)
        p.drawPath(cuerpo)


# ---------------------------------------------------------------------------
# Orbe de voz
# ---------------------------------------------------------------------------

COLOR_FASE = {"escuchando": CIAN, "pensando": VIOLETA, "hablando": VERDE, "inactivo": "#4A5578", "cargando": AMBAR}


class OrbeVoz(QAbstractButton):
    """Botón de micrófono. Fases: inactivo, cargando, escuchando, pensando, hablando."""

    def __init__(self, tamano: int = 60, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(tamano, tamano)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Activar o desactivar el micrófono")
        self._fase_nombre = "inactivo"
        self._t = 0.0
        self._reloj = Reloj(self, self._animar, 33)
        self._reloj.start()

    def set_fase(self, fase: str) -> None:
        self._fase_nombre = fase if fase in COLOR_FASE else "inactivo"

    def _animar(self) -> None:
        self._t = (self._t + 0.045) % 1000
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(60, 60)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        c = QPointF(self.width() / 2, self.height() / 2)
        radio = min(self.width(), self.height()) / 2 - 4
        tono = QColor(COLOR_FASE[self._fase_nombre])
        activo = self._fase_nombre != "inactivo"
        t = self._t

        if self._fase_nombre == "escuchando":
            for k in range(2):
                f = ((t * 0.7) + k * 0.5) % 1.0
                p.setPen(QPen(color(tono.name(), 0.45 * (1 - f)), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, radio * (0.78 + 0.32 * f) + 2, radio * (0.78 + 0.32 * f) + 2)
        elif self._fase_nombre == "hablando":
            for k in range(3):
                f = ((t * 1.6) + k / 3) % 1.0
                p.setPen(QPen(color(tono.name(), 0.5 * (1 - f)), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, radio * (0.8 + 0.3 * f) + 1, radio * (0.8 + 0.3 * f) + 1)

        cuerpo = QRadialGradient(QPointF(c.x() - radio * 0.3, c.y() - radio * 0.4), radio * 1.5)
        if activo:
            cuerpo.setColorAt(0, tono.lighter(135))
            cuerpo.setColorAt(1, tono.darker(135))
        else:
            cuerpo.setColorAt(0, QColor(PANEL3))
            cuerpo.setColorAt(1, QColor(PANEL2))
        p.setPen(QPen(tono if activo else QColor(BORDE), 1.5))
        p.setBrush(QBrush(cuerpo))
        p.drawEllipse(c, radio * 0.78, radio * 0.78)

        if self._fase_nombre == "pensando":
            p.setPen(QPen(QColor("white"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for k in range(3):
                a = (t * 2.4 + k / 3) * 2 * math.pi
                caja = QRectF(c.x() - radio * 0.5, c.y() - radio * 0.5, radio, radio)
                p.drawArc(caja, int(-math.degrees(a) * 16), 40 * 16)
            return

        tinta = QColor("white") if activo else QColor(TEXTO2)
        self._microfono(p, c, radio * 0.78, tinta, self._fase_nombre == "hablando", t)

    @staticmethod
    def _microfono(p: QPainter, c: QPointF, r: float, tinta: QColor, barras: bool, t: float) -> None:
        if barras:
            p.setPen(QPen(tinta, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(5):
                alto = r * (0.25 + 0.45 * abs(math.sin(t * 5 + i * 0.9)))
                x = c.x() + (i - 2) * r * 0.27
                p.drawLine(QPointF(x, c.y() - alto / 2), QPointF(x, c.y() + alto / 2))
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tinta)
        p.drawRoundedRect(QRectF(c.x() - r * 0.15, c.y() - r * 0.5, r * 0.30, r * 0.52), r * 0.15, r * 0.15)
        p.setPen(QPen(tinta, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(c.x() - r * 0.30, c.y() - r * 0.30, r * 0.60, r * 0.50), 200 * 16, 140 * 16)
        p.drawLine(QPointF(c.x(), c.y() + r * 0.20), QPointF(c.x(), c.y() + r * 0.42))
        p.drawLine(QPointF(c.x() - r * 0.14, c.y() + r * 0.42), QPointF(c.x() + r * 0.14, c.y() + r * 0.42))


# ---------------------------------------------------------------------------
# Botón del modo guardián
# ---------------------------------------------------------------------------

class BotonGuardian(QAbstractButton):
    """Estados: 'apagado', 'armado', 'alarma'."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(190, 46)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._estado = "apagado"
        self._t = 0.0
        self._reloj = Reloj(self, self._animar, 33)
        self._reloj.start()

    def set_estado(self, estado: str) -> None:
        self._estado = estado
        self.setToolTip({"apagado": "Activar el modo guardián", "armado": "Desactivar el modo guardián",
                         "alarma": "Apagar la alarma"}[estado])

    def _animar(self) -> None:
        self._t = (self._t + 0.12) % 1000
        if self._estado != "apagado":
            self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        pulso = 0.5 + 0.5 * math.sin(self._t)
        if self._estado == "alarma":
            base, etiqueta = QColor(ROJO), "¡Alarma!"
            relleno = color(ROJO, 0.30 + 0.35 * pulso)
        elif self._estado == "armado":
            base, etiqueta = QColor(CIAN), "Guardián activo"
            relleno = color(CIAN, 0.16)
        else:
            base, etiqueta = QColor("#4A5578"), "Guardián apagado"
            relleno = color(PANEL2)
        p.setPen(QPen(base, 1.6))
        p.setBrush(relleno)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)

        # Escudo
        cx, cy, s = r.left() + 28, r.center().y(), 11
        escudo = QPainterPath()
        escudo.moveTo(cx, cy - s)
        escudo.lineTo(cx + s * 0.85, cy - s * 0.6)
        escudo.lineTo(cx + s * 0.85, cy + s * 0.1)
        escudo.cubicTo(cx + s * 0.8, cy + s * 0.7, cx + s * 0.3, cy + s * 1.0, cx, cy + s * 1.15)
        escudo.cubicTo(cx - s * 0.3, cy + s * 1.0, cx - s * 0.8, cy + s * 0.7, cx - s * 0.85, cy + s * 0.1)
        escudo.lineTo(cx - s * 0.85, cy - s * 0.6)
        escudo.closeSubpath()
        p.setPen(QPen(base, 2))
        p.setBrush(base if self._estado != "apagado" else Qt.BrushStyle.NoBrush)
        p.drawPath(escudo)
        if self._estado != "apagado":
            p.setPen(QPen(QColor(FONDO), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx - 4, cy + 1), QPointF(cx - 1, cy + 4))
            p.drawLine(QPointF(cx - 1, cy + 4), QPointF(cx + 5, cy - 3))

        p.setPen(QColor(TEXTO) if self._estado != "apagado" else QColor(TEXTO2))
        p.setFont(_fuente(10.5, 650))
        p.drawText(QRectF(r.left() + 50, r.top(), r.width() - 56, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, etiqueta)


# ---------------------------------------------------------------------------
# Píldora de conexión
# ---------------------------------------------------------------------------

class PildoraEstado(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._texto, self._color = "Conectando…", AMBAR
        self.setFixedHeight(34)
        self._t = 0.0
        self._reloj = Reloj(self, self._animar, 50)
        self._reloj.start()

    def set_estado(self, texto: str, tono: str) -> None:
        self._texto, self._color = texto, tono
        self.setFixedWidth(QFontMetricsAncho(texto) + 46)
        self.update()

    def _animar(self) -> None:
        self._t += 0.12
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(color(self._color, 0.6), 1.2))
        p.setBrush(color(self._color, 0.10))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        pulso = 0.6 + 0.4 * math.sin(self._t)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color(self._color, 0.25 * pulso))
        p.drawEllipse(QPointF(r.left() + 18, r.center().y()), 7, 7)
        p.setBrush(QColor(self._color))
        p.drawEllipse(QPointF(r.left() + 18, r.center().y()), 4, 4)
        p.setPen(QColor(TEXTO))
        p.setFont(_fuente(10, 550))
        p.drawText(QRectF(r.left() + 32, r.top(), r.width() - 36, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._texto)


def QFontMetricsAncho(texto: str) -> int:
    return QFontMetrics(_fuente(10, 550)).horizontalAdvance(texto)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class Burbuja(QFrame):
    """Un mensaje del chat. rol: usuario | asistente | alerta | sistema."""

    def __init__(self, rol: str, texto: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rol = rol
        self.etiqueta = QLabel(texto)
        self.etiqueta.setWordWrap(True)
        self.etiqueta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pie = QLabel("")
        self.pie.setStyleSheet(f"color: {TEXTO2}; font-size: 10px;")
        self.pie.hide()

        columna = QVBoxLayout(self)
        columna.setContentsMargins(0, 0, 0, 0)
        columna.setSpacing(3)

        marco = QFrame()
        marco.setObjectName("marco")
        interior = QVBoxLayout(marco)
        interior.setContentsMargins(14, 9, 14, 9)
        interior.addWidget(self.etiqueta)

        if rol == "usuario":
            marco.setStyleSheet(f"#marco {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {CIAN}, stop:1 {VIOLETA});"
                                f" border-radius: 16px; border-bottom-right-radius: 4px; }}")
            self.etiqueta.setStyleSheet("color: #06101E; font-size: 13.5px; font-weight: 600;")
            alineacion = Qt.AlignmentFlag.AlignRight
        elif rol == "alerta":
            marco.setStyleSheet(f"#marco {{ background: rgba(255,77,109,0.16); border: 1px solid {ROJO}; border-radius: 14px; }}")
            self.etiqueta.setStyleSheet(f"color: #FFC2CC; font-size: 13.5px; font-weight: 700;")
            alineacion = Qt.AlignmentFlag.AlignLeft
        elif rol == "sistema":
            marco.setStyleSheet("#marco { background: transparent; }")
            self.etiqueta.setStyleSheet(f"color: {TEXTO2}; font-size: 11.5px; font-style: italic;")
            self.etiqueta.setAlignment(Qt.AlignmentFlag.AlignCenter)
            alineacion = Qt.AlignmentFlag.AlignHCenter
        else:
            marco.setStyleSheet(f"#marco {{ background: {PANEL3}; border: 1px solid {BORDE}; border-radius: 16px;"
                                f" border-bottom-left-radius: 4px; }}")
            self.etiqueta.setStyleSheet("font-size: 13.5px;")
            alineacion = Qt.AlignmentFlag.AlignLeft

        fila = QHBoxLayout()
        fila.setContentsMargins(0, 0, 0, 0)
        if alineacion in (Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignHCenter):
            fila.addStretch(1)
        fila.addWidget(marco)
        if alineacion in (Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignHCenter):
            fila.addStretch(1)
        columna.addLayout(fila)
        if rol in ("asistente", "alerta"):
            pie = QHBoxLayout()
            pie.setContentsMargins(6, 0, 0, 0)
            pie.addWidget(self.pie)
            pie.addStretch(1)
            columna.addLayout(pie)

    def poner_pie(self, texto: str) -> None:
        self.pie.setText(texto)
        self.pie.show()

    def limitar_ancho(self, maximo: int) -> None:
        """
        Ancho del texto: el natural si cabe, y si no el máximo (y se parte en líneas).
        Hay que fijar el mínimo: una etiqueta con salto de línea automático, si no,
        se encoge al ancho de una palabra y recorta el texto.
        """
        maximo = max(120, maximo)
        fuente = QFont(FUENTE)
        fuente.setPixelSize(14)          # en píxeles, como el estilo (13.5px); en puntos no coincide según el DPI
        fuente.setBold(self.rol in ("usuario", "alerta"))
        natural = QFontMetrics(fuente).horizontalAdvance(self.etiqueta.text()) + 20
        self.etiqueta.setMaximumWidth(maximo)
        self.etiqueta.setMinimumWidth(min(natural, maximo))


class PanelChat(QWidget):
    """Conversación con el asistente. Señales: texto_enviado(str), microfono_pulsado()."""

    texto_enviado = Signal(str)
    microfono_pulsado = Signal()

    SUGERENCIAS = ["Prende la sala", "Apaga todo", "¿Qué temperatura hace?", "Pon música"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(10)

        # Cabecera: nombre del asistente y fase actual
        cabecera = QHBoxLayout()
        self.estado = QLabel("Conversación")
        self.estado.setStyleSheet("font-size: 15px; font-weight: 700;")
        self.fase = QLabel("")
        self.fase.setStyleSheet(f"color: {TEXTO2}; font-size: 12px;")
        cabecera.addWidget(self.estado)
        cabecera.addStretch(1)
        cabecera.addWidget(self.fase)
        raiz.addLayout(cabecera)

        # Mensajes
        self._desplazable = QScrollArea()
        self._desplazable.setWidgetResizable(True)
        self._desplazable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        contenedor = QWidget()
        contenedor.setObjectName("contenedor_chat")
        contenedor.setStyleSheet("#contenedor_chat { background: transparent; }")
        self._desplazable.viewport().setStyleSheet("background: transparent;")
        self._mensajes = QVBoxLayout(contenedor)
        self._mensajes.setContentsMargins(2, 2, 6, 2)
        self._mensajes.setSpacing(8)
        self._mensajes.addStretch(1)
        self._desplazable.setWidget(contenedor)
        raiz.addWidget(self._desplazable, 1)
        self._burbujas: list[Burbuja] = []
        self._ultima_asistente: Burbuja | None = None

        # Sugerencias
        sugerencias = QHBoxLayout()
        sugerencias.setSpacing(6)
        for frase in self.SUGERENCIAS:
            b = QPushButton(frase)
            b.setObjectName("chip")
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b.clicked.connect(lambda _=False, f=frase: self.texto_enviado.emit(f))
            sugerencias.addWidget(b)
        sugerencias.addStretch(1)
        raiz.addLayout(sugerencias)

        # Entrada
        entrada = QHBoxLayout()
        entrada.setSpacing(8)
        self.campo = QLineEdit()
        self.campo.setPlaceholderText("Escribe una orden o pulsa el micrófono…")
        self.campo.returnPressed.connect(self._enviar)
        enviar = QPushButton("➤")
        enviar.setObjectName("enviar")
        enviar.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        enviar.clicked.connect(self._enviar)
        self.orbe = OrbeVoz(52)
        self.orbe.clicked.connect(self.microfono_pulsado)
        entrada.addWidget(self.campo, 1)
        entrada.addWidget(enviar)
        entrada.addWidget(self.orbe)
        raiz.addLayout(entrada)

    def _enviar(self) -> None:
        texto = self.campo.text().strip()
        if texto:
            self.campo.clear()
            self.texto_enviado.emit(texto)

    def agregar(self, rol: str, texto: str) -> Burbuja:
        burbuja = Burbuja(rol, texto)
        burbuja.limitar_ancho(int(self._desplazable.viewport().width() * 0.70))
        self._mensajes.insertWidget(self._mensajes.count() - 1, burbuja)
        self._burbujas.append(burbuja)
        if rol in ("asistente", "alerta"):
            self._ultima_asistente = burbuja
        QTimer.singleShot(30, self._bajar)
        return burbuja

    def poner_tiempos(self, texto: str) -> None:
        if self._ultima_asistente is not None:
            self._ultima_asistente.poner_pie(texto)

    def _bajar(self) -> None:
        barra = self._desplazable.verticalScrollBar()
        barra.setValue(barra.maximum())

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        maximo = int(self._desplazable.viewport().width() * 0.70)
        for b in self._burbujas:
            b.limitar_ancho(maximo)

    def set_fase(self, fase: str) -> None:
        texto, tono = {
            "escuchando": ("Escuchando…", CIAN), "pensando": ("Pensando…", VIOLETA), "hablando": ("Hablando…", VERDE),
            "cargando": ("Cargando el micrófono…", AMBAR), "inactivo": ("Solo texto", TEXTO2),
        }.get(fase, ("", TEXTO2))
        self.fase.setText(f"<span style='color:{tono}'>●</span>&nbsp; {texto}" if texto else "")
        self.orbe.set_fase(fase)


# ---------------------------------------------------------------------------
# Música
# ---------------------------------------------------------------------------

class Ecualizador(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(34, 24)
        self._sonando = False
        self._t = 0.0
        self._reloj = Reloj(self, self._animar, 40)
        self._reloj.start()

    def set_sonando(self, sonando: bool) -> None:
        self._sonando = sonando

    def _animar(self) -> None:
        self._t += 0.28
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        _suavizado(p)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(4):
            alto = 0.25 + 0.75 * abs(math.sin(self._t * (1 + i * 0.37) + i)) if self._sonando else 0.18
            h = self.height() * alto
            p.setBrush(QColor(AMBAR) if self._sonando else QColor("#3A4670"))
            p.drawRoundedRect(QRectF(i * 8 + 1, self.height() - h, 5, h), 2.5, 2.5)


class PanelMusica(QWidget):
    reproducir = Signal(int)       # número de pista del SD
    detener = Signal()
    volumen = Signal(int)          # 0..VOLUMEN_MAX

    def __init__(self, canciones: list[tuple[int, str]], parent: QWidget | None = None) -> None:
        """`canciones`: lista de (pista, título) en el orden del catálogo."""
        super().__init__(parent)
        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(8)

        cabecera = QHBoxLayout()
        self.sonando = QLabel("Nada sonando")
        self.sonando.setStyleSheet("font-size: 15px; font-weight: 700;")
        self.eq = Ecualizador()
        cabecera.addWidget(self.sonando, 1)
        cabecera.addWidget(self.eq)
        raiz.addLayout(cabecera)

        self.lista = QListWidget()
        self.lista.setMinimumHeight(110)
        self._titulos: dict[int, str] = {}
        for numero, (pista, titulo) in enumerate(canciones, start=1):
            item = QListWidgetItem(f"{numero:02d}     {titulo}")
            item.setData(Qt.ItemDataRole.UserRole, pista)
            self.lista.addItem(item)
            self._titulos[pista] = titulo
        self.lista.itemClicked.connect(lambda it: self.reproducir.emit(int(it.data(Qt.ItemDataRole.UserRole))))
        if not canciones:
            self.sonando.setText("Sin canciones cargadas")
        raiz.addWidget(self.lista, 1)

        controles = QHBoxLayout()
        controles.setSpacing(10)
        parar = QPushButton("■  Detener")
        parar.setObjectName("secundario")
        parar.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        parar.clicked.connect(self.detener)
        self.deslizador = QSlider(Qt.Orientation.Horizontal)
        self.deslizador.setRange(0, VOLUMEN_MAX)
        self.deslizador.setValue(VOLUMEN_MAX // 2)
        self.etiqueta_volumen = QLabel("50%")
        self.etiqueta_volumen.setFixedWidth(40)
        self.etiqueta_volumen.setStyleSheet(f"color: {TEXTO2};")
        self.deslizador.valueChanged.connect(lambda v: self.etiqueta_volumen.setText(f"{round(v * 100 / VOLUMEN_MAX)}%"))
        self.deslizador.sliderReleased.connect(lambda: self.volumen.emit(self.deslizador.value()))
        icono = QLabel("🔈")
        controles.addWidget(parar)
        controles.addSpacing(6)
        controles.addWidget(icono)
        controles.addWidget(self.deslizador, 1)
        controles.addWidget(self.etiqueta_volumen)
        raiz.addLayout(controles)

    def set_estado(self, pista: int, volumen: int, hay_mp3: bool, alarma: bool) -> None:
        if alarma:
            self.sonando.setText("🚨  Sonando la alarma")
        elif not hay_mp3:
            self.sonando.setText("Reproductor no detectado")
        elif pista:
            self.sonando.setText(f"♪  {self._titulos.get(pista, f'Pista {pista}')}")
        else:
            self.sonando.setText("Nada sonando")
        self.eq.set_sonando(bool(pista) and hay_mp3)
        for i in range(self.lista.count()):
            item = self.lista.item(i)
            item.setForeground(QColor(AMBAR) if item.data(Qt.ItemDataRole.UserRole) == pista and pista else QColor(TEXTO))
        if not self.deslizador.isSliderDown():
            self.deslizador.blockSignals(True)
            self.deslizador.setValue(volumen)
            self.deslizador.blockSignals(False)
            self.etiqueta_volumen.setText(f"{round(volumen * 100 / VOLUMEN_MAX)}%")
        self.lista.setEnabled(hay_mp3 and not alarma)
