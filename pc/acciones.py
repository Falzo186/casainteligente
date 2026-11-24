"""
acciones.py — Ejecuta un Comando en la casa (a través de serial_client) y
devuelve la frase que hay que decir por voz.

    Resultado(mensaje="Luces de la sala encendidas.", tiempo_s=0.03)

Errores:
  - ErrorConexionCasa  se propaga: la placa no responde (main/GUI lo avisan).
  - ErrorRespuestaCasa se traduce a una frase amable ("La alarma está sonando").
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from catalogo import Catalogo
from config import VOLUMEN_MAX, VOLUMEN_PASO, ZONAS_CASA
from nlu import Comando
from serial_client import ClienteSerie, ErrorRespuestaCasa, EstadoCasa

log = logging.getLogger("acciones")


@dataclass(frozen=True)
class Resultado:
    mensaje: str
    tiempo_s: float          # tiempo de comunicación con la placa


def _de(nombre: str) -> str:
    """'el baño' -> 'del baño'; 'la sala' -> 'de la sala'."""
    return "del " + nombre[3:] if nombre.startswith("el ") else "de " + nombre


def _lista(nombres: list[str]) -> str:
    return nombres[0] if len(nombres) == 1 else ", ".join(nombres[:-1]) + " y " + nombres[-1]


def _decimal(valor: float, decimales: int = 1) -> str:
    """24.5 -> '24,5' (así lo lee mejor la voz en español)."""
    return f"{valor:.{decimales}f}".replace(".", ",")


def _porcentaje(volumen: int) -> int:
    return round(volumen * 100 / VOLUMEN_MAX)


class Ejecutor:

    def __init__(self, cliente: ClienteSerie, catalogo: Catalogo, azar: random.Random | None = None) -> None:
        self._casa = cliente
        self._catalogo = catalogo
        self._azar = azar or random.Random()
        self._ultima_aleatoria: int | None = None
        self._ultima_por_lista: dict[str, int] = {}

    def ejecutar(self, c: Comando) -> Resultado:
        if not c.ejecutable:
            raise ValueError(f"El comando no es ejecutable: {c}")
        t0 = time.perf_counter()
        try:
            mensaje = self._despachar(c)
        except ErrorRespuestaCasa as e:
            log.warning("La placa rechazó la orden: %s", e)
            mensaje = self._explicar_error(str(e))
        return Resultado(mensaje, time.perf_counter() - t0)

    @staticmethod
    def _explicar_error(motivo: str) -> str:
        if "alarma" in motivo:
            return "La alarma está sonando. Apágala primero."
        if "mp3" in motivo:
            return "El reproductor de música no está conectado."
        return f"La casa no aceptó la orden: {motivo}."

    def _despachar(self, c: Comando) -> str:
        objetivo, accion = c.objetivo, c.accion
        encender = accion == "encender"

        if objetivo == "luces":
            for zona in c.zonas:
                self._casa.set_led(ZONAS_CASA[zona][0], encender)
            estado = "encendidas" if encender else "apagadas"
            if len(c.zonas) == len(ZONAS_CASA):
                return f"Todas las luces {estado}."
            nombres = [ZONAS_CASA[z][1] for z in c.zonas]
            return f"Luces {_de(nombres[0])}{' y ' + _lista(nombres[1:]) if len(nombres) > 1 else ''} {estado}."

        if objetivo == "ventilador":
            self._casa.set_ventilador(encender)
            return f"Ventilador {'encendido' if encender else 'apagado'}."

        if objetivo == "todo":
            self._casa.set_todos_los_leds(encender)
            self._casa.set_ventilador(encender)
            if not encender:
                try:
                    self._casa.detener()
                except ErrorRespuestaCasa:
                    pass          # sin reproductor o con la alarma sonando: no importa aquí
            return "Todo encendido." if encender else "Todo apagado."

        if objetivo == "guardian":
            self._casa.set_guardian(encender)
            return f"Modo guardián {'activado' if encender else 'desactivado'}."

        if objetivo == "alarma":
            self._casa.apagar_alarma()
            return "Alarma apagada y modo guardián desactivado."

        if objetivo == "musica":
            return self._musica(c)

        if objetivo == "volumen":
            return self._volumen(c)

        if accion == "consultar":
            return self._consultar(objetivo)

        raise ValueError(f"Comando sin implementar: {c}")

    # --- Música y volumen ------------------------------------------------------

    def _musica(self, c: Comando) -> str:
        if c.accion == "detener":
            self._casa.detener()
            return "Música detenida."
        pista = c.cancion
        if pista is None and c.lista:
            lista = self._catalogo.por_nombre_lista(c.lista)
            if lista is None:
                return "No tengo esa lista."
            evitar = self._ultima_por_lista.get(c.lista)
            opciones = [p for p in lista.pistas if p != evitar] or list(lista.pistas)
            pista = self._azar.choice(opciones)
            self._ultima_por_lista[c.lista] = pista
            self._casa.reproducir(pista)
            return f"Reproduciendo algo de {lista.nombre}."
        if pista is None:
            if len(self._catalogo) == 0:
                return "No tengo canciones cargadas."
            opciones = [x.pista for x in self._catalogo.canciones if x.pista != self._ultima_aleatoria] \
                or [x.pista for x in self._catalogo.canciones]
            pista = self._azar.choice(opciones)
            self._ultima_aleatoria = pista
        self._casa.reproducir(pista)
        cancion = self._catalogo.por_pista(pista)
        return f"Reproduciendo {cancion.titulo}." if cancion else f"Reproduciendo la pista {pista}."

    def _volumen(self, c: Comando) -> str:
        actual = self._casa.obtener_estado().volumen
        if c.accion == "fijar":
            nuevo = c.valor if c.valor is not None else actual
        elif c.accion == "subir":
            nuevo = min(VOLUMEN_MAX, actual + VOLUMEN_PASO)
        else:
            nuevo = max(0, actual - VOLUMEN_PASO)
        nuevo = max(0, min(VOLUMEN_MAX, nuevo))
        if nuevo == actual and c.accion in ("subir", "bajar"):
            return "El volumen ya está al máximo." if c.accion == "subir" else "El volumen ya está al mínimo."
        self._casa.set_volumen(nuevo)
        return f"Volumen al {_porcentaje(nuevo)} por ciento."

    # --- Consultas -------------------------------------------------------------

    def _consultar(self, objetivo: str | None) -> str:
        e = self._casa.obtener_estado()
        if objetivo == "temperatura":
            if e.temperatura is None:
                return "No puedo leer el sensor de temperatura."
            return f"La temperatura es de {_decimal(e.temperatura)} grados."
        if objetivo == "humedad":
            if e.humedad is None:
                return "No puedo leer el sensor de humedad."
            return f"La humedad es del {round(e.humedad)} por ciento."
        return self._resumen(e)

    @staticmethod
    def _resumen(e: EstadoCasa) -> str:
        partes = []
        if e.temperatura is not None and e.humedad is not None:
            partes.append(f"Hay {_decimal(e.temperatura)} grados y {round(e.humedad)} por ciento de humedad.")
        encendidas = [ZONAS_CASA[z][1] for z, on in zip(ZONAS_CASA, e.leds) if on]
        partes.append("Luces encendidas: " + _lista(encendidas) + "." if encendidas else "Todas las luces están apagadas.")
        partes.append(f"El ventilador está {'encendido' if e.ventilador else 'apagado'}.")
        if e.alarma:
            partes.append("¡La alarma está sonando!")
        else:
            partes.append(f"El modo guardián está {'activado' if e.guardian else 'desactivado'}.")
        return " ".join(partes)
