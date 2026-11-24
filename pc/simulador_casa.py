"""
simulador_casa.py — La placa de la casa simulada en Python.

Implementa el mismo protocolo que firmware/casa_serial/casa_serial.ino, así se
puede probar el cliente, el diálogo y la interfaz sin hardware:

    from simulador_casa import CasaSimulada
    casa = CasaSimulada()
    cliente = ClienteSerie(fabrica_puerto=casa.abrir)

También se usa como "modo demo" de la interfaz. Los métodos públicos
(disparar_intruso, terminar_pista, ...) permiten provocar situaciones.
"""

from __future__ import annotations

import json
import queue
import random
import threading
import time

NUM_LEDS = 6
PISTA_ALARMA = 1
PISTA_MAX = 30
_CERRADO = object()


class PuertoSimulado:
    """Imita a serial.Serial: write() hacia la placa, readline() desde la placa."""

    def __init__(self, casa: CasaSimulada, timeout: float) -> None:
        self._casa = casa
        self.timeout = timeout
        self._salida: queue.Queue = queue.Queue()
        self.is_open = True

    def write(self, datos: bytes) -> int:
        if not self.is_open:
            raise OSError("puerto cerrado")
        self._casa._recibir(self, datos)
        return len(datos)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        try:
            item = self._salida.get(timeout=self.timeout)
        except queue.Empty:
            return b""
        if item is _CERRADO:
            self._salida.put(_CERRADO)   # por si lo leen varios
            raise OSError("dispositivo desconectado")
        return item

    def close(self) -> None:
        self.is_open = False
        self._salida.put(_CERRADO)
        self._casa._cerrado(self)

    def _entregar(self, linea: str) -> None:
        if self.is_open:
            self._salida.put((linea + "\n").encode("utf-8"))


class CasaSimulada:
    """Estado y lógica de la placa. Seguro para usar desde varios hilos."""

    def __init__(self, con_mp3: bool = True, ruido: bool = False) -> None:
        self._lock = threading.RLock()
        self._puerto: PuertoSimulado | None = None
        self._inicio = time.monotonic()
        self.con_mp3 = con_mp3
        self.ruido = ruido                 # temperatura y humedad que varían un poco
        self.silenciosa = False            # True: no responde (para probar timeouts)
        self.temperatura: float | None = 24.5
        self.humedad: float | None = 58.0
        self.pir = False
        self.leds = [False] * NUM_LEDS
        self.ventilador = False
        self.guardian = False
        self.alarma = False
        self.pista = 0
        self.volumen = 14
        self.eventos = 0
        self.recibidas: list[str] = []     # historial de órdenes (para los tests)

    # --- Conexión -----------------------------------------------------------

    def abrir(self, nombre: str = "simulado", baudios: int = 115200, timeout: float = 0.1) -> PuertoSimulado:
        """Fábrica de puerto: equivale a enchufar el cable (y a que la placa arranque)."""
        with self._lock:
            if self._puerto is not None:
                self._puerto.close()
            puerto = PuertoSimulado(self, timeout)
            self._puerto = puerto
            for linea in ("", "=== CASA DOMOTICA ESP32 LISTA ===", "MP3: OK!" if self.con_mp3 else "MP3: No detectado."):
                puerto._entregar(linea)
            return puerto

    def desenchufar(self) -> None:
        """Simula quitar el cable USB: el lector del cliente recibe un error."""
        with self._lock:
            puerto, self._puerto = self._puerto, None
        if puerto is not None:
            puerto.is_open = False
            puerto._salida.put(_CERRADO)

    def _cerrado(self, puerto: PuertoSimulado) -> None:
        with self._lock:
            if self._puerto is puerto:
                self._puerto = None

    def _decir(self, linea: str) -> None:
        with self._lock:
            if self._puerto is not None:
                self._puerto._entregar(linea)

    # --- Situaciones para probar -------------------------------------------

    def disparar_intruso(self) -> None:
        """El PIR detecta movimiento: con el guardián activo suena la alarma."""
        with self._lock:
            self.pir = True
            if self.guardian and not self.alarma:
                self.alarma = True
                self.eventos += 1
                if self.con_mp3:
                    self.pista = PISTA_ALARMA
                self._decir("EVT INTRUSO")

    def fin_de_movimiento(self) -> None:
        with self._lock:
            self.pir = False

    def terminar_pista(self) -> None:
        """La canción llegó a su fin."""
        with self._lock:
            if self.pista and not self.alarma:
                pista, self.pista = self.pista, 0
                self._decir(f"EVT FIN {pista}")

    # --- Protocolo ----------------------------------------------------------

    def _recibir(self, puerto: PuertoSimulado, datos: bytes) -> None:
        for linea in datos.decode("ascii", errors="replace").splitlines():
            if linea.strip():
                self._procesar(linea.strip())

    def estado_json(self) -> str:
        with self._lock:
            t, h = self.temperatura, self.humedad
            if self.ruido and t is not None and h is not None:
                t, h = t + random.uniform(-0.3, 0.3), h + random.uniform(-1, 1)
            return json.dumps({
                "t": None if t is None else round(t, 1), "h": None if h is None else round(h),
                "pir": int(self.pir), "leds": [int(x) for x in self.leds], "fan": int(self.ventilador),
                "guard": int(self.guardian), "alarm": int(self.alarma), "mp3": int(self.con_mp3),
                "track": self.pista, "vol": self.volumen, "ev": self.eventos,
                "up": int((time.monotonic() - self._inicio) * 1000),
            }, separators=(",", ":"))

    @staticmethod
    def _enteros(texto: str, n: int) -> list[int] | None:
        partes = texto.split()
        if len(partes) != n:
            return None
        try:
            return [int(p) for p in partes]
        except ValueError:
            return None

    def _procesar(self, texto: str) -> None:
        with self._lock:
            self.recibidas.append(texto)
            if self.silenciosa:
                return
            verbo, _, resto = texto.upper().partition(" ")
            resto = resto.strip()
            self._decir(self._ejecutar(verbo, resto))

    def _ejecutar(self, verbo: str, resto: str) -> str:
        if verbo == "PING":
            return "PONG"
        if verbo == "STATUS":
            return self.estado_json()
        if verbo == "HELP":
            return "OK PING STATUS LED LEDS FAN GUARD ALARMOFF PLAY STOP VOL"

        if verbo == "LED":
            v = self._enteros(resto, 2)
            if v is None or not 1 <= v[0] <= NUM_LEDS or v[1] not in (0, 1):
                return "ERR uso: LED <1-6> <0|1>"
            self.leds[v[0] - 1] = bool(v[1])
            return f"OK LED {v[0]} {v[1]}"
        if verbo in ("LEDS", "FAN", "GUARD"):
            v = self._enteros(resto, 1)
            if v is None or v[0] not in (0, 1):
                return f"ERR uso: {verbo} <0|1>"
            if verbo == "LEDS":
                self.leds = [bool(v[0])] * NUM_LEDS
            elif verbo == "FAN":
                self.ventilador = bool(v[0])
            elif v[0] == 1:
                self.guardian = True
            else:
                self._desactivar_alarma()
            return f"OK {verbo} {v[0]}"
        if verbo == "ALARMOFF":
            self._desactivar_alarma()
            return "OK ALARMOFF"
        if verbo == "PLAY":
            v = self._enteros(resto, 1)
            if v is None or not 1 <= v[0] <= PISTA_MAX:
                return "ERR uso: PLAY <1-30>"
            if self.alarma:
                return "ERR alarma activa"
            if not self.con_mp3:
                return "ERR mp3 no detectado"
            self.pista = v[0]
            return f"OK PLAY {v[0]}"
        if verbo == "STOP":
            if not self.con_mp3:
                return "ERR mp3 no detectado"
            self.pista = 0
            return "OK STOP"
        if verbo == "VOL":
            v = self._enteros(resto, 1)
            if v is None or not 0 <= v[0] <= 30:
                return "ERR uso: VOL <0-30>"
            self.volumen = v[0]
            return f"OK VOL {v[0]}"
        return "ERR comando desconocido"

    def _desactivar_alarma(self) -> None:
        self.guardian = False
        if self.alarma:
            self.pista = 0
        self.alarma = False
