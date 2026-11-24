"""
main.py — Asistente de la Casa Inteligente en la consola (sin interfaz gráfica).

    micrófono -> Whisper -> reglas (+ LLM de respaldo) -> placa por USB -> voz

Es lo mismo que hace gui_app.py, pero sin ventana. Úsalo para probar el
micrófono y la voz, o en un equipo sin pantalla. Cada orden registra el tiempo
de cada etapa:

    Tiempos: STT 0.85 s | reglas 0.3 ms | placa 25.0 ms | voz 1.30 s | total 2.30 s

Uso (desde la carpeta pc/):

    python main.py            # con micrófono y la placa por USB
    python main.py --texto    # escribiendo las órdenes en el teclado
    python main.py --demo     # con la placa simulada (sin hardware)
    python main.py --debug    # más detalle en el registro
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys

import config
from acciones import Ejecutor
from catalogo import Catalogo, ErrorCatalogo
from config import DIALOGO, LOG, SERIE, STT
from dialogo import Asistente, FuenteTeclado, FuenteVoz
from entendedor import Entendedor
from serial_client import ClienteSerie, ErrorConexionCasa, EstadoCasa, EventoCasa, MonitorCasa
from stt import ErrorMicrofono, ErrorSTT
from tts import Sintetizador

log = logging.getLogger("main")


def _configurar_logging(debug: bool) -> None:
    for flujo in (sys.stdout, sys.stderr):   # la consola de Windows no siempre es UTF-8
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    nivel = logging.DEBUG if debug else getattr(logging, LOG.nivel.upper(), logging.INFO)
    logging.basicConfig(level=nivel, format=LOG.formato, datefmt=LOG.formato_fecha)
    for ruidosa in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(ruidosa).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(description="Casa Inteligente en Caja (consola)")
    analizador.add_argument("--texto", action="store_true", help="escribir las órdenes en vez de hablarlas")
    analizador.add_argument("--demo", action="store_true", help="usar la placa simulada (sin hardware)")
    analizador.add_argument("--debug", action="store_true", help="registro detallado")
    args = analizador.parse_args(argv)
    _configurar_logging(args.debug)

    problemas = config.validar()
    if problemas:
        for p in problemas:
            log.error("Configuración inválida: %s", p)
        return 1

    try:
        catalogo = Catalogo.cargar()
    except ErrorCatalogo as e:
        log.error("%s", e)
        return 1
    log.info("Canciones: %d | modelo de respaldo: %s | Whisper: %s", len(catalogo), config.LLM.modelo, STT.modelo)

    # --- La casa (placa real o simulada) ---
    placa = None
    if args.demo:
        from simulador_casa import CasaSimulada
        placa = CasaSimulada(ruido=True)
    cliente = ClienteSerie(fabrica_puerto=placa.abrir if placa else None)
    try:
        cliente.conectar()
    except ErrorConexionCasa as e:
        # No es fatal: el cable puede conectarse después; el monitor reintenta cada segundo.
        log.warning("La placa no está disponible todavía: %s", e)

    # --- Comprensión: las reglas siempre; el modelo es un respaldo opcional ---
    entendedor = Entendedor(catalogo)
    if entendedor.verificar():
        entendedor.precargar()
    ejecutor = Ejecutor(cliente, catalogo)

    # --- Entrada ---
    fuente = FuenteTeclado() if args.texto else FuenteVoz()
    try:
        if not args.texto:
            log.info("Cargando Whisper '%s' (la primera vez se descarga)...", STT.modelo)
        fuente.iniciar()
    except (ErrorMicrofono, ErrorSTT) as e:
        log.error("%s", e)
        if isinstance(e, ErrorMicrofono):
            log.error("Puedes probar sin micrófono con: python main.py --texto")
        cliente.cerrar()
        return 1

    # --- Alertas: eventos de la placa y pérdida de conexión ---
    alertas: queue.Queue[str] = queue.Queue()
    cliente.suscribir(lambda ev: alertas.put(DIALOGO.mensaje_alerta_intruso) if ev.tipo == "intruso" else None)
    armado = {"si": False}

    def al_estado(e: EstadoCasa) -> None:
        armado["si"] = e.guardian

    def al_conexion(ok: bool, texto: str) -> None:
        if not ok and armado["si"]:
            alertas.put(DIALOGO.mensaje_conexion_perdida)

    monitor = MonitorCasa(cliente, al_estado, al_conexion, intervalo_s=SERIE.intervalo_estado_s)
    monitor.start()

    asistente = Asistente(fuente, Sintetizador(), entendedor, ejecutor, alertas)
    try:
        asistente.decir(DIALOGO.mensaje_inicio)
        log.info("Escuchando. Di, por ejemplo: «prende la luz de la sala» o «pon música».")
        asistente.ejecutar_bucle()
    except KeyboardInterrupt:
        print()
    finally:
        log.info("Cerrando...")
        monitor.parar()
        fuente.detener()
        cliente.cerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
