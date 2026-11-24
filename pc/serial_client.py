"""
serial_client.py — Cliente del puerto serie para la placa (firmware/casa_serial).

Protocolo de líneas (ver la cabecera de casa_serial.ino):

    PC -> placa:   PING | STATUS | LED n v | LEDS v | FAN v | GUARD v | ALARMOFF |
                   PLAY n | STOP | VOL v
    placa -> PC:   PONG | OK ... | ERR motivo | {json de STATUS}   (respuestas)
                   EVT INTRUSO | EVT FIN n | EVT ERROR codigo       (espontáneos)

Un hilo lector separa las respuestas de los eventos, así un aviso de intruso
llega al instante aunque no se haya preguntado nada. Todas las órdenes fijan un
estado (no alternan), por lo que repetirlas es seguro.

Piezas:
  - ClienteSerie: conexión, órdenes, STATUS y suscripción a eventos.
  - MonitorCasa:  hilo que pide STATUS periódicamente y reconecta solo.

Prueba manual (placa conectada, o --demo para usar la placa simulada):

    python serial_client.py estado
    python serial_client.py led 4 on
    python serial_client.py vigilar
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from config import SERIE, ConfigSerie

try:
    import serial
    from serial.tools import list_ports
except ImportError:   # permite importar el módulo (y probarlo) sin pyserial
    serial = None
    list_ports = None

log = logging.getLogger("serie")

NUM_LEDS = 6
# Chips USB-serie habituales de las placas ESP32: Silicon Labs CP210x, WCH CH340,
# FTDI y el USB nativo de Espressif.
VID_PLACAS = {0x10C4, 0x1A86, 0x0403, 0x303A}


# ---------------------------------------------------------------------------
# Excepciones y datos
# ---------------------------------------------------------------------------

class ErrorCasa(Exception):
    """Error genérico al hablar con la placa."""


class ErrorConexionCasa(ErrorCasa):
    """No hay placa, el puerto no abre, no responde o se desconectó."""


class ErrorRespuestaCasa(ErrorCasa):
    """La placa respondió ERR (orden inválida o no permitida en este momento)."""


@dataclass(frozen=True)
class EventoCasa:
    tipo: str                    # "intruso", "fin", "error_musica", "desconexion"
    dato: int | None = None      # número de pista en "fin"; código DFPlayer en "error_musica"
    t: float = field(default_factory=time.time)


@dataclass(frozen=True)
class EstadoCasa:
    """Respuesta de STATUS. Tolera campos ausentes o nulos."""

    temperatura: float | None = None
    humedad: float | None = None
    movimiento: bool = False
    leds: tuple[bool, ...] = (False,) * NUM_LEDS
    ventilador: bool = False
    guardian: bool = False
    alarma: bool = False
    mp3: bool = False
    pista: int = 0               # 0 = nada sonando
    volumen: int = 0
    eventos: int = 0             # contador de intrusiones desde el arranque
    uptime_ms: int = 0

    @classmethod
    def desde_json(cls, d: dict[str, Any]) -> EstadoCasa:
        def numero(valor: Any) -> float | None:
            try:
                return None if valor is None else float(valor)
            except (TypeError, ValueError):
                return None

        def entero(valor: Any) -> int:
            try:
                return int(valor)
            except (TypeError, ValueError):
                return 0

        leds = d.get("leds")
        if not isinstance(leds, list):
            leds = []
        leds = (list(map(bool, leds)) + [False] * NUM_LEDS)[:NUM_LEDS]
        return cls(
            temperatura=numero(d.get("t")), humedad=numero(d.get("h")),
            movimiento=bool(d.get("pir")), leds=tuple(leds),
            ventilador=bool(d.get("fan")), guardian=bool(d.get("guard")),
            alarma=bool(d.get("alarm")), mp3=bool(d.get("mp3")),
            pista=entero(d.get("track")), volumen=entero(d.get("vol")),
            eventos=entero(d.get("ev")), uptime_ms=entero(d.get("up")),
        )


def elegir_puerto(puertos: list[Any]) -> str:
    """
    Elige el puerto de la placa entre los disponibles (objetos con .device,
    .vid y .description, como los de pyserial). Lanza ErrorConexionCasa si no
    hay ninguno o no se puede decidir.
    """
    if not puertos:
        raise ErrorConexionCasa(
            "No se encontró ningún puerto serie. Conecta la placa por USB "
            "(y revisa el driver CP210x/CH340) o fija CASA_PUERTO=COM5."
        )
    de_placa = [p for p in puertos if getattr(p, "vid", None) in VID_PLACAS]
    if len(de_placa) >= 1:
        if len(de_placa) > 1:
            log.warning("Hay varias placas posibles (%s); se usa %s. Fija CASA_PUERTO para elegir.",
                        ", ".join(p.device for p in de_placa), de_placa[0].device)
        return de_placa[0].device
    if len(puertos) == 1:
        return puertos[0].device
    raise ErrorConexionCasa(
        "Hay varios puertos serie y ninguno parece una placa ESP32: "
        + ", ".join(f"{p.device} ({p.description})" for p in puertos)
        + ". Fija CASA_PUERTO con el correcto."
    )


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

class ClienteSerie:
    """
    Uso:
        with ClienteSerie() as casa:
            casa.set_led(4, True)
            estado = casa.obtener_estado()
            casa.suscribir(lambda ev: print(ev))

    `fabrica_puerto(nombre, baudios, timeout)` permite inyectar un puerto falso
    en los tests; por defecto abre un serial.Serial real.
    """

    def __init__(self, cfg: ConfigSerie = SERIE, fabrica_puerto: Callable[..., Any] | None = None) -> None:
        self._cfg = cfg
        self._fabrica = fabrica_puerto
        self._puerto: Any = None
        self._lock = threading.RLock()           # una orden a la vez
        self._respuestas: queue.Queue[str] = queue.Queue()
        self._suscriptores: list[Callable[[EventoCasa], None]] = []
        self._hilo: threading.Thread | None = None
        self._parar = threading.Event()
        self.nombre_puerto: str | None = None

    def __enter__(self) -> ClienteSerie:
        self.conectar()
        return self

    def __exit__(self, *exc) -> None:
        self.cerrar()

    @property
    def conectado(self) -> bool:
        return self._puerto is not None

    # --- Eventos ------------------------------------------------------------

    def suscribir(self, callback: Callable[[EventoCasa], None]) -> None:
        """Registra una función a la que se llama (en el hilo lector) con cada evento."""
        self._suscriptores.append(callback)

    def _emitir(self, evento: EventoCasa) -> None:
        log.info("Evento: %s %s", evento.tipo, evento.dato if evento.dato is not None else "")
        for callback in list(self._suscriptores):
            try:
                callback(evento)
            except Exception:
                log.exception("Error en un suscriptor de eventos")

    # --- Conexión -----------------------------------------------------------

    def _abrir(self, nombre: str) -> Any:
        if self._fabrica is not None:
            return self._fabrica(nombre, self._cfg.baudios, self._cfg.timeout_lectura_s)
        if serial is None:
            raise ErrorConexionCasa("pyserial no está instalado (pip install pyserial).")
        return serial.Serial(nombre, self._cfg.baudios, timeout=self._cfg.timeout_lectura_s)

    def _resolver_puerto(self) -> str:
        if self._cfg.puerto.strip().lower() != "auto":
            return self._cfg.puerto.strip()
        if self._fabrica is not None:   # puerto inyectado (tests / modo demo)
            return "simulado"
        if list_ports is None:
            raise ErrorConexionCasa("pyserial no está instalado (pip install pyserial).")
        return elegir_puerto(list(list_ports.comports()))

    def conectar(self) -> None:
        """Abre el puerto y espera a que la placa responda a PING."""
        with self._lock:
            if self.conectado:
                return
            nombre = self._resolver_puerto()
            try:
                self._puerto = self._abrir(nombre)
            except Exception as e:   # SerialException, PermissionError (puerto en uso), etc.
                self._puerto = None
                raise ErrorConexionCasa(
                    f"No se pudo abrir {nombre}: {e}. ¿Está abierto el Monitor Serie del Arduino IDE?"
                ) from e
            self.nombre_puerto = nombre
            self._parar.clear()
            self._vaciar_respuestas()
            self._hilo = threading.Thread(target=self._leer, args=(self._puerto,), daemon=True, name="lector-serie")
            self._hilo.start()

            # La placa puede estar reiniciándose por abrir el puerto: se reintenta PING.
            limite = time.monotonic() + self._cfg.espera_arranque_s
            while time.monotonic() < limite and self._puerto is not None:
                try:
                    if self._transaccion("PING", 0.5) == "PONG":
                        log.info("Placa conectada en %s", nombre)
                        return
                except ErrorConexionCasa:
                    pass
            self._cerrar_puerto()
            raise ErrorConexionCasa(
                f"La placa no responde en {nombre}. ¿Está cargado casa_serial.ino y es el puerto correcto?"
            )

    def cerrar(self) -> None:
        with self._lock:
            self._cerrar_puerto()

    def _cerrar_puerto(self) -> None:
        self._parar.set()
        puerto, self._puerto = self._puerto, None
        if puerto is not None:
            try:
                puerto.close()
            except Exception as e:
                log.debug("Error al cerrar el puerto: %s", e)

    def _marcar_desconectado(self, motivo: Exception | str) -> None:
        """Lo llama el hilo lector (o una escritura fallida) cuando el puerto se pierde."""
        if self._puerto is None:
            return
        log.error("Conexión con la placa perdida: %s", motivo)
        self._cerrar_puerto()
        self._emitir(EventoCasa("desconexion"))

    # --- Hilo lector --------------------------------------------------------

    def _leer(self, puerto: Any) -> None:
        pendiente = b""
        while not self._parar.is_set():
            try:
                datos = puerto.readline()
            except Exception as e:
                if puerto is self._puerto:
                    self._marcar_desconectado(e)
                return
            if not datos:
                continue
            pendiente += datos
            # Una lectura puede traer varias líneas (o media): se parte por saltos
            # de línea y lo que quede a medias espera a la siguiente lectura.
            while b"\n" in pendiente:
                bruta, pendiente = pendiente.split(b"\n", 1)
                linea = bruta.decode("utf-8", errors="replace").strip()
                if linea:
                    self._clasificar(linea)

    def _clasificar(self, linea: str) -> None:
        if linea.startswith("EVT "):
            partes = linea.split()
            if partes[1] == "INTRUSO":
                self._emitir(EventoCasa("intruso"))
            elif partes[1] == "FIN":
                try:
                    pista = int(partes[2])
                except (IndexError, ValueError):
                    pista = None
                self._emitir(EventoCasa("fin", pista))
            elif partes[1] == "ERROR":
                try:
                    codigo = int(partes[2])
                except (IndexError, ValueError):
                    codigo = None
                self._emitir(EventoCasa("error_musica", codigo))
            else:
                log.debug("Evento desconocido: %s", linea)
        elif linea in ("PONG", "OK") or linea.startswith(("OK ", "ERR", "{")):
            self._respuestas.put(linea)
        else:
            log.debug("placa: %s", linea)   # mensajes de arranque, menú, basura del bootloader

    # --- Órdenes ------------------------------------------------------------

    def _vaciar_respuestas(self) -> None:
        while True:
            try:
                self._respuestas.get_nowait()
            except queue.Empty:
                return

    def _transaccion(self, orden: str, timeout_s: float) -> str:
        """Envía una línea y devuelve la siguiente respuesta (sin interpretar ERR)."""
        with self._lock:
            puerto = self._puerto
            if puerto is None:
                raise ErrorConexionCasa("Sin conexión con la placa.")
            self._vaciar_respuestas()
            try:
                puerto.write((orden + "\n").encode("ascii"))
                puerto.flush()
            except Exception as e:
                self._marcar_desconectado(e)
                raise ErrorConexionCasa(f"No se pudo escribir en el puerto: {e}") from e
            try:
                return self._respuestas.get(timeout=timeout_s)
            except queue.Empty:
                if self._puerto is None:
                    raise ErrorConexionCasa("Se perdió la conexión con la placa.") from None
                raise ErrorConexionCasa(f"La placa no respondió a '{orden}' en {timeout_s:.1f} s.") from None

    def _enviar(self, orden: str) -> str:
        """Orden normal: reconecta si hace falta y convierte ERR en excepción."""
        with self._lock:
            if not self.conectado:
                self.conectar()
            t0 = time.perf_counter()
            respuesta = self._transaccion(orden, self._cfg.timeout_respuesta_s)
            log.debug("%s -> %s (%.0f ms)", orden, respuesta[:60], (time.perf_counter() - t0) * 1000)
        if respuesta.startswith("ERR"):
            raise ErrorRespuestaCasa(respuesta[4:] or "error")
        return respuesta

    @staticmethod
    def _bit(valor: bool) -> int:
        return 1 if valor else 0

    def set_led(self, numero: int, encendido: bool) -> None:
        if not 1 <= numero <= NUM_LEDS:
            raise ValueError(f"Foco inválido: {numero} (1 a {NUM_LEDS})")
        self._enviar(f"LED {numero} {self._bit(encendido)}")

    def set_todos_los_leds(self, encendido: bool) -> None:
        self._enviar(f"LEDS {self._bit(encendido)}")

    def set_ventilador(self, encendido: bool) -> None:
        self._enviar(f"FAN {self._bit(encendido)}")

    def set_guardian(self, activo: bool) -> None:
        self._enviar(f"GUARD {self._bit(activo)}")

    def apagar_alarma(self) -> None:
        self._enviar("ALARMOFF")

    def reproducir(self, pista: int) -> None:
        if not 1 <= pista <= 30:
            raise ValueError(f"Pista inválida: {pista} (1 a 30)")
        self._enviar(f"PLAY {pista}")

    def detener(self) -> None:
        self._enviar("STOP")

    def set_volumen(self, volumen: int) -> None:
        self._enviar(f"VOL {max(0, min(30, int(volumen)))}")

    def obtener_estado(self) -> EstadoCasa:
        respuesta = self._enviar("STATUS")
        try:
            datos = json.loads(respuesta)
        except json.JSONDecodeError as e:
            raise ErrorRespuestaCasa(f"STATUS no es JSON válido: {respuesta[:60]!r}") from e
        if not isinstance(datos, dict):
            raise ErrorRespuestaCasa("STATUS no es un objeto JSON")
        return EstadoCasa.desde_json(datos)


# ---------------------------------------------------------------------------
# Monitor: STATUS periódico y reconexión automática
# ---------------------------------------------------------------------------

class MonitorCasa(threading.Thread):
    """
    Pide STATUS cada `intervalo_estado_s` y avisa de los cambios:

        on_estado(EstadoCasa)          en cada lectura correcta
        on_conexion(bool, str)         solo cuando cambia (conectada / perdida)

    Si la placa se desconecta, reintenta conectar en cada vuelta, así basta con
    volver a enchufar el cable. Los callbacks se llaman desde este hilo.
    """

    def __init__(
        self, cliente: ClienteSerie, on_estado: Callable[[EstadoCasa], None] | None = None,
        on_conexion: Callable[[bool, str], None] | None = None, intervalo_s: float | None = None,
    ) -> None:
        super().__init__(name="monitor-casa", daemon=True)
        self._cliente = cliente
        self._on_estado = on_estado
        self._on_conexion = on_conexion
        self._intervalo = SERIE.intervalo_estado_s if intervalo_s is None else intervalo_s
        self._parar_evento = threading.Event()
        self._conectado: bool | None = None   # None = aún no se sabe

    def parar(self) -> None:
        self._parar_evento.set()

    def run(self) -> None:
        while True:
            try:
                self.sondear()
            except Exception:
                log.exception("Error inesperado en el monitor de la casa")
            if self._parar_evento.wait(self._intervalo):
                return

    def sondear(self) -> None:
        """Una vuelta (separada de run() para poder probarla)."""
        try:
            estado = self._cliente.obtener_estado()
        except ErrorCasa as e:
            self._cambiar_conexion(False, str(e))
            return
        self._cambiar_conexion(True, f"Conectada en {self._cliente.nombre_puerto}")
        if self._on_estado is not None:
            self._on_estado(estado)

    def _cambiar_conexion(self, conectado: bool, mensaje: str) -> None:
        if conectado == self._conectado:
            return
        self._conectado = conectado
        (log.info if conectado else log.warning)("Placa %s: %s", "conectada" if conectado else "sin conexión", mensaje)
        if self._on_conexion is not None:
            self._on_conexion(conectado, mensaje)


# ---------------------------------------------------------------------------
# Prueba manual desde consola
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    analizador = argparse.ArgumentParser(description="Cliente serie de la casa")
    analizador.add_argument("--demo", action="store_true", help="usar la placa simulada")
    sub = analizador.add_subparsers(dest="orden", required=True)
    sub.add_parser("estado")
    p_led = sub.add_parser("led")
    p_led.add_argument("numero", type=int)
    p_led.add_argument("estado", choices=("on", "off"))
    sub.add_parser("ventilador").add_argument("estado", choices=("on", "off"))
    sub.add_parser("guardian").add_argument("estado", choices=("on", "off"))
    sub.add_parser("play").add_argument("pista", type=int)
    sub.add_parser("stop")
    sub.add_parser("vigilar")
    args = analizador.parse_args()

    fabrica = None
    if args.demo:
        from simulador_casa import CasaSimulada
        fabrica = CasaSimulada().abrir

    try:
        with ClienteSerie(fabrica_puerto=fabrica) as casa:
            if args.orden == "estado":
                print(casa.obtener_estado())
            elif args.orden == "led":
                casa.set_led(args.numero, args.estado == "on")
                print("OK")
            elif args.orden == "ventilador":
                casa.set_ventilador(args.estado == "on")
                print("OK")
            elif args.orden == "guardian":
                casa.set_guardian(args.estado == "on")
                print("OK")
            elif args.orden == "play":
                casa.reproducir(args.pista)
                print("OK")
            elif args.orden == "stop":
                casa.detener()
                print("OK")
            elif args.orden == "vigilar":
                casa.suscribir(lambda ev: print(f"EVENTO: {ev.tipo} {ev.dato or ''}"))
                print("Vigilando (Ctrl+C para salir)...")
                while True:
                    print(casa.obtener_estado())
                    time.sleep(SERIE.intervalo_estado_s)
    except ErrorCasa as e:
        raise SystemExit(f"Error: {e}")
    except KeyboardInterrupt:
        print()
