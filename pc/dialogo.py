"""
dialogo.py — El asistente: conversación, aclaraciones, voz y alertas.

Une las piezas:

    entrada (micrófono o teclado) -> Entendedor -> Ejecutor (placa) -> voz

y mantiene el contexto de la aclaración pendiente: si el usuario dice "prende
las luces", se pregunta "¿Qué luces quieres encender?" y la siguiente frase se
fusiona con el comando pendiente. Lo usan tanto la consola (main.py) como la
interfaz gráfica (gui_app.py).

Los eventos de la conversación salen por `al_evento(tipo, texto, **meta)`:
    "oi"      lo que dijo el usuario            "dije"   lo que responde el asistente
    "alerta"  aviso de seguridad                "sistema" avisos técnicos
    meta["tiempos"]  "reglas 0.3 ms · placa 25 ms · voz 1.2 s" (solo en "dije")
Y los cambios de fase por `al_fase(fase)`: "escuchando", "pensando", "hablando".
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from config import DIALOGO, STT
from entendedor import Entendedor
from nlu import Comando
from serial_client import ErrorConexionCasa, EventoCasa
from stt import ErrorMicrofono, ErrorSTT, ReconocedorVoz
from acciones import Ejecutor

log = logging.getLogger("dialogo")
log_tiempos = logging.getLogger("tiempos")


# ---------------------------------------------------------------------------
# Fuentes de entrada
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entrada:
    texto: str
    audio_s: float | None = None   # duración del audio (solo micrófono)
    stt_s: float | None = None     # lo que tardó Whisper (solo micrófono)
    origen: str = "voz"            # "voz" o "teclado"


class FinDeEntrada(Exception):
    """La entrada de teclado se cerró."""


class FuenteVoz:
    """Entrada por micrófono: envuelve a stt.ReconocedorVoz."""

    def __init__(self, reconocedor: ReconocedorVoz | None = None) -> None:
        self._stt = reconocedor or ReconocedorVoz()

    def iniciar(self) -> None:
        self._stt.iniciar()

    def escuchar(self, timeout: float) -> Entrada | None:
        r = self._stt.escuchar(timeout=timeout)
        return None if r is None else Entrada(r.texto, r.duracion_audio_s, r.tiempo_transcripcion_s, "voz")

    def descartar(self) -> None:
        """Olvida el audio acumulado (para no oír la propia voz de la PC)."""
        self._stt.descartar_audio()

    def recuperar(self) -> None:
        self._stt.reiniciar_microfono()

    def detener(self) -> None:
        self._stt.detener()


class FuenteTeclado:
    """Entrada por teclado (consola): un hilo lee las líneas y las deja en una cola."""

    def __init__(self) -> None:
        self._cola: queue.Queue[str | None] = queue.Queue()

    def iniciar(self) -> None:
        print("Modo texto: escribe las órdenes y pulsa Enter (Ctrl+C para salir).")
        threading.Thread(target=self._leer, daemon=True).start()

    def _leer(self) -> None:
        while True:
            try:
                linea = input("> ")
            except EOFError:
                self._cola.put(None)
                return
            if linea.strip():
                self._cola.put(linea.strip())

    def escuchar(self, timeout: float) -> Entrada | None:
        try:
            texto = self._cola.get(timeout=timeout)
        except queue.Empty:
            return None
        if texto is None:
            raise FinDeEntrada()
        return Entrada(texto, origen="teclado")

    def descartar(self) -> None:
        pass

    def recuperar(self) -> None:
        pass

    def detener(self) -> None:
        pass


class FuenteCombinada:
    """
    Texto escrito (chat de la interfaz) + micrófono opcional. El micrófono se
    puede activar y desactivar en caliente; el texto escrito tiene prioridad.
    """

    def __init__(self, voz: FuenteVoz | None = None) -> None:
        self._texto: queue.Queue[str] = queue.Queue()
        self.voz = voz

    def enviar_texto(self, texto: str) -> None:
        if texto.strip():
            self._texto.put(texto.strip())

    def escuchar(self, timeout: float) -> Entrada | None:
        try:
            return Entrada(self._texto.get_nowait(), origen="teclado")
        except queue.Empty:
            pass
        voz = self.voz
        if voz is None:
            try:
                return Entrada(self._texto.get(timeout=timeout), origen="teclado")
            except queue.Empty:
                return None
        # Con micrófono se escucha en tramos cortos para atender el texto escrito.
        return voz.escuchar(min(timeout, 0.25))

    def descartar(self) -> None:
        if self.voz is not None:
            self.voz.descartar()

    def recuperar(self) -> None:
        if self.voz is not None:
            self.voz.recuperar()

    def detener(self) -> None:
        if self.voz is not None:
            self.voz.detener()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _plano(texto: str) -> str:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", sin_tildes).split())


def es_cancelacion(texto: str) -> bool:
    """True si la frase es 'cancela', 'olvídalo', 'nada, gracias'..."""
    plano = _plano(texto)
    for frase in DIALOGO.frases_cancelar:
        f = _plano(frase)
        if plano == f or plano.startswith(f + " "):
            return True
    return False


def texto_alerta(evento: EventoCasa) -> str | None:
    """Frase para un evento de la placa (None si no hay que anunciarlo)."""
    if evento.tipo == "intruso":
        return DIALOGO.mensaje_alerta_intruso
    return None


# ---------------------------------------------------------------------------
# Asistente
# ---------------------------------------------------------------------------

class Asistente:
    """
    Recibe las frases y ejecuta el ciclo completo. Todo se inyecta para poder
    probarlo sin hardware:
      fuente      FuenteVoz / FuenteTeclado / FuenteCombinada
      tts         objeto con hablar(texto)
      entendedor  Entendedor      ejecutor  Ejecutor
    """

    def __init__(
        self, fuente: Any, tts: Any, entendedor: Entendedor, ejecutor: Ejecutor,
        alertas: queue.Queue | None = None,
        al_evento: Callable[..., None] | None = None, al_fase: Callable[[str], None] | None = None,
        timeout_respuesta_s: float | None = None, pausa_microfono_s: float | None = None,
    ) -> None:
        self.fuente = fuente
        self.tts = tts
        self.entendedor = entendedor
        self.ejecutor = ejecutor
        self.alertas = alertas if alertas is not None else queue.Queue()
        self._al_evento = al_evento
        self._al_fase = al_fase
        self._timeout_respuesta = STT.timeout_escucha_s if timeout_respuesta_s is None else timeout_respuesta_s
        self._pausa_microfono = (
            DIALOGO.pausa_reintento_microfono_s if pausa_microfono_s is None else pausa_microfono_s
        )
        self._parar = threading.Event()
        self._microfono_avisado = False
        self.pendiente: Comando | None = None
        self.pendiente_desde = 0.0
        self.intentos = 0

    # --- Salida -------------------------------------------------------------

    def _evento(self, tipo: str, texto: str, **meta: Any) -> None:
        if self._al_evento is not None:
            try:
                self._al_evento(tipo, texto, **meta)
            except Exception:
                log.exception("Error en al_evento")

    def _fase(self, fase: str) -> None:
        if self._al_fase is not None:
            try:
                self._al_fase(fase)
            except Exception:
                log.exception("Error en al_fase")

    def decir(self, texto: str, tiempos: dict[str, float] | None = None, tipo: str = "dije") -> None:
        """Muestra y dice el texto, y descarta el audio captado mientras hablaba (para no oírse)."""
        meta: dict[str, Any] = {}
        t0 = time.perf_counter()
        self._fase("hablando")
        self._evento(tipo, texto, **meta)
        self.tts.hablar(texto)
        if tiempos is not None:
            tiempos["voz"] = time.perf_counter() - t0
        self.fuente.descartar()
        self._fase("escuchando")

    def _atender_alertas(self) -> bool:
        mensajes: list[str] = []
        while True:
            try:
                mensajes.append(self.alertas.get_nowait())
            except queue.Empty:
                break
        if mensajes:
            self.decir(" ".join(dict.fromkeys(mensajes)), tipo="alerta")
        return bool(mensajes)

    def avisar(self, texto: str) -> None:
        """Encola un aviso hablado (lo puede llamar cualquier hilo)."""
        self.alertas.put(texto)

    # --- Bucle --------------------------------------------------------------

    def parar(self) -> None:
        self._parar.set()

    def ejecutar_bucle(self) -> None:
        """Bucle principal: se detiene con parar(), Ctrl+C o al cerrarse el teclado."""
        errores_seguidos = 0
        self._fase("escuchando")
        while not self._parar.is_set():
            try:
                self.ciclo()
                errores_seguidos = 0
            except FinDeEntrada:
                log.info("Entrada cerrada.")
                return
            except ErrorSTT as e:
                log.error("Fallo del reconocimiento de voz: %s", e)
                self._evento("sistema", f"Fallo del reconocimiento de voz: {e}")
                self._parar.wait(1.0)
            except Exception:
                errores_seguidos += 1
                log.exception("Error inesperado en el ciclo principal (%d)", errores_seguidos)
                self._parar.wait(min(5.0, errores_seguidos))

    def ciclo(self) -> None:
        """Una iteración: espera una frase (o una alerta) y la procesa."""
        total = self._timeout_respuesta if self.pendiente is not None else DIALOGO.intervalo_alertas_s
        try:
            entrada = self._escuchar(total)
        except ErrorMicrofono as e:
            self._limpiar()
            self._recuperar_microfono(e)
            return

        if entrada is not None:
            self.procesar(entrada)
        elif self.pendiente is not None:
            log.info("Sin respuesta a la aclaración: se cancela %s", self.pendiente)
            self._limpiar()
            self.decir(DIALOGO.mensaje_sin_respuesta)

    def _escuchar(self, total_s: float) -> Entrada | None:
        """Escucha hasta `total_s` s en tramos cortos, atendiendo alertas (que reinician la espera)."""
        limite = time.monotonic() + total_s
        while True:
            restante = limite - time.monotonic()
            entrada = self.fuente.escuchar(max(0.0, min(DIALOGO.intervalo_alertas_s, restante)))
            if entrada is not None:
                return entrada
            if self._atender_alertas():
                limite = time.monotonic() + total_s
            if time.monotonic() >= limite:
                return None

    def _recuperar_microfono(self, error: ErrorMicrofono) -> None:
        log.error("Micrófono: %s", error)
        if not self._microfono_avisado:
            self._microfono_avisado = True
            self.decir(DIALOGO.mensaje_sin_microfono)
        while not self._parar.is_set():
            self._atender_alertas()
            if self._parar.wait(self._pausa_microfono):
                return
            try:
                self.fuente.recuperar()
            except ErrorMicrofono as e:
                log.warning("El micrófono sigue sin estar disponible: %s", e)
                continue
            log.info("Micrófono recuperado.")
            self._microfono_avisado = False
            return

    # --- Una frase ----------------------------------------------------------

    def procesar(self, entrada: Entrada) -> None:
        """Interpreta una frase, la ejecuta (o pregunta) y responde."""
        t_inicio = time.perf_counter()
        tiempos: dict[str, float] = {}
        if entrada.stt_s is not None:
            tiempos["STT"] = entrada.stt_s
        texto = entrada.texto
        audio = f" (audio {entrada.audio_s:.1f} s)" if entrada.audio_s is not None else ""
        log.info('Oí: "%s"%s', texto, audio)
        self._evento("oi", texto, origen=entrada.origen)
        self._fase("pensando")

        if self.pendiente is not None and time.monotonic() - self.pendiente_desde > DIALOGO.timeout_aclaracion_s:
            log.info("La aclaración pendiente caducó; la frase se trata como un comando nuevo.")
            self._limpiar()

        if self.pendiente is not None and es_cancelacion(texto):
            self._limpiar()
            respuesta = DIALOGO.mensaje_cancelado
        else:
            respuesta = self._interpretar_y_ejecutar(texto, tiempos)

        self.decir(respuesta, tiempos)
        self._registrar_tiempos(tiempos, time.perf_counter() - t_inicio, respuesta)

    def _interpretar_y_ejecutar(self, texto: str, tiempos: dict[str, float]) -> str:
        t0 = time.perf_counter()
        comando = self.entendedor.interpretar(texto, self.pendiente)
        tiempos[self.entendedor.ultimo_origen] = time.perf_counter() - t0
        log.info("Comando: %s", comando)
        previo = self.pendiente

        if comando.es_ambiguo:
            if previo is not None and comando == previo:
                return self._sin_progreso(comando)
            self._esperar_aclaracion(comando)
            return comando.pregunta_aclaracion or DIALOGO.mensaje_no_entendido

        if comando.accion == "desconocido":
            if comando.nota:                      # "no hago nada", "dime una orden a la vez"...
                self._limpiar()
                return comando.nota
            if previo is not None:
                return self._sin_progreso(previo)
            return DIALOGO.mensaje_no_entendido

        self._limpiar()
        return self._ejecutar(comando, tiempos)

    def _ejecutar(self, comando: Comando, tiempos: dict[str, float]) -> str:
        try:
            resultado = self.ejecutor.ejecutar(comando)
        except ErrorConexionCasa as e:
            log.error("%s", e)
            return DIALOGO.mensaje_error_red
        except ValueError as e:
            log.warning("Comando no ejecutable: %s", e)
            return DIALOGO.mensaje_no_entendido
        tiempos["placa"] = resultado.tiempo_s
        log.info("Casa: %s", resultado.mensaje)
        return resultado.mensaje

    # --- Contexto de aclaración ---------------------------------------------

    def _esperar_aclaracion(self, comando: Comando) -> None:
        if self.pendiente != comando:
            self.intentos = 0
        self.pendiente = comando
        self.pendiente_desde = time.monotonic()

    def _sin_progreso(self, pendiente: Comando) -> str:
        self.intentos += 1
        if self.intentos >= DIALOGO.max_intentos_aclaracion:
            log.info("Sin aclaración tras %d intentos: se cancela %s", self.intentos, pendiente)
            self._limpiar()
            return DIALOGO.mensaje_sin_aclaracion
        self.pendiente_desde = time.monotonic()
        return pendiente.pregunta_aclaracion or DIALOGO.mensaje_no_entendido

    def _limpiar(self) -> None:
        self.pendiente = None
        self.intentos = 0

    # --- Tiempos ------------------------------------------------------------

    def _registrar_tiempos(self, tiempos: dict[str, float], total_s: float, respuesta: str) -> None:
        def formato(nombre: str, valor: float) -> str:
            return f"{nombre} {valor * 1000:.1f} ms" if valor < 0.1 else f"{nombre} {valor:.2f} s"

        etapas = " | ".join(formato(n, v) for n, v in tiempos.items())
        log_tiempos.info("Tiempos: %s | total %.2f s", etapas, total_s)
        # Para la interfaz: "reglas 0.3 ms · placa 25 ms · voz 1.20 s"
        self._evento("tiempos", " · ".join(formato(n, v) for n, v in tiempos.items()))
