"""
entendedor.py — De la frase al Comando: reglas primero, LLM de respaldo.

    frase ──> nlu.analizar() ──reconoce──> Comando            (< 1 ms, no inventa)
                   │
                   └─ no reconoce nada ──> Ollama (1.5B) ──> fundamentar() ──> Comando

Por qué así, con un modelo tan pequeño:
  - Las reglas resuelven casi todo al instante y de forma predecible.
  - El modelo solo entiende paráfrasis que las reglas no cubren
    ("ilumina el baño", "hace mucho calor"), y su salida se RESTRINGE con un
    esquema JSON (Ollama la genera con gramática: no puede devolver valores
    fuera de las listas ni JSON roto).
  - Aun así se "fundamenta": zonas, canciones y números solo valen si están en
    las palabras del usuario, porque un modelo pequeño rellena huecos.
  - Si Ollama no está disponible, las reglas siguen funcionando.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from typing import Any

from catalogo import Catalogo, normalizar
from config import LLM, ZONAS_CASA
from nlu import ACCIONES, OBJETIVOS, Comando, Hallazgos, _CONSULTA, _zonas, analizar, completar, describir

try:
    import ollama
except ImportError:
    ollama = None

log = logging.getLogger("entendedor")

_ACCIONES_LLM = [a for a in ACCIONES if a != "desconocido"] + ["desconocido"]
_OBJETIVOS_LLM = list(OBJETIVOS) + ["ninguno"]

# Esquema de la salida: Ollama genera SOLO JSON que lo cumpla.
ESQUEMA = {
    "type": "object",
    "properties": {
        "accion": {"type": "string", "enum": _ACCIONES_LLM},
        "objetivo": {"type": "string", "enum": _OBJETIVOS_LLM},
        "zonas": {"type": "array", "items": {"type": "string", "enum": list(ZONAS_CASA)}},
        "cancion": {"type": ["string", "null"]},
        "valor": {"type": ["integer", "null"]},
    },
    "required": ["accion", "objetivo", "zonas", "cancion", "valor"],
}

PROMPT_SISTEMA = f"""Interpretas órdenes para una casa inteligente y respondes SOLO con JSON.
accion: {", ".join(_ACCIONES_LLM)}.
objetivo: {", ".join(_OBJETIVOS_LLM)}.
zonas (solo para luces): {", ".join(ZONAS_CASA)}.
cancion: texto con el título pedido, o null. valor: número del volumen, o null.
Usa SOLO lo que el usuario dijo; si no dice la zona, zonas es [].
Ejemplos:
"ilumina el baño" -> {{"accion":"encender","objetivo":"luces","zonas":["bano"],"cancion":null,"valor":null}}
"refréscame un poco" -> {{"accion":"encender","objetivo":"ventilador","zonas":[],"cancion":null,"valor":null}}
"ya no quiero el abanico" -> {{"accion":"apagar","objetivo":"ventilador","zonas":[],"cancion":null,"valor":null}}
"quiero oír algo de música" -> {{"accion":"reproducir","objetivo":"musica","zonas":[],"cancion":null,"valor":null}}
"cuéntame un chiste" -> {{"accion":"desconocido","objetivo":"ninguno","zonas":[],"cancion":null,"valor":null}}"""


def fundamentar(llm: Hallazgos, texto: str, catalogo: Catalogo) -> Hallazgos:
    """
    Descarta lo que el modelo "adivinó" y el usuario no dijo.
    Conserva la acción y el objetivo (para eso se usa el modelo: entender
    paráfrasis), pero zonas, canción y número deben estar en la frase.
    """
    plano = normalizar(texto)
    cancion = catalogo.buscar(plano)
    lista = catalogo.buscar_lista(plano) if cancion is None else None
    tiene_numero = bool(re.search(r"\d", plano))
    objetivo, accion = llm.objetivo, llm.accion
    # "qué hora es" no es una consulta de la casa: hace falta una palabra de consulta.
    if objetivo in ("temperatura", "humedad", "estado") and not _CONSULTA.search(plano):
        objetivo, accion = None, None
    return Hallazgos(
        accion=accion, objetivo=objetivo,
        zonas=_zonas(plano) if llm.objetivo in (None, "luces", "todo") else (),
        cancion=cancion.pista if cancion else None,
        aleatoria=False,
        lista=lista.nombre if lista else None,
        valor=llm.valor if tiene_numero else None,
    )


# Acciones con sentido para cada objetivo. El modelo pequeño a veces devuelve
# combinaciones absurdas ("reproducir ventilador", "consultar luces").
_ACCIONES_VALIDAS = {
    "luces": {"encender", "apagar"}, "ventilador": {"encender", "apagar"}, "todo": {"encender", "apagar"},
    "guardian": {"encender", "apagar"}, "alarma": {"apagar"},
    "musica": {"reproducir", "detener"}, "volumen": {"subir", "bajar", "fijar"},
    "temperatura": {"consultar"}, "humedad": {"consultar"}, "estado": {"consultar"},
}
# Con estos objetivos, una acción incoherente se convierte en "pregunta qué hacer";
# con los demás (que ejecutarían algo sin más) se descarta toda la interpretación.
_PREGUNTAR_SI_INCOHERENTE = {"luces", "ventilador", "todo"}


def _a_hallazgos(datos: dict[str, Any]) -> Hallazgos:
    """JSON del modelo -> Hallazgos (valores fuera de las listas o incoherentes se ignoran)."""
    accion = datos.get("accion")
    objetivo = datos.get("objetivo")
    valor = datos.get("valor")
    accion = accion if accion in ACCIONES and accion != "desconocido" else None
    objetivo = objetivo if objetivo in OBJETIVOS else None
    if objetivo is not None and accion not in _ACCIONES_VALIDAS[objetivo]:
        if objetivo in _PREGUNTAR_SI_INCOHERENTE:
            accion = None
        else:
            accion, objetivo = None, None
    return Hallazgos(
        accion=accion, objetivo=objetivo,
        valor=valor if isinstance(valor, int) and not isinstance(valor, bool) else None,
    )


class Entendedor:
    """
    Uso:
        entendedor = Entendedor(Catalogo.cargar())
        comando = entendedor.interpretar("prende la luz de la sala")
        # si comando.es_ambiguo: decir comando.pregunta_aclaracion y volver a llamar
        # con la respuesta:  entendedor.interpretar("la cocina", pendiente=comando)
    """

    def __init__(self, catalogo: Catalogo, cliente: Any = None, usar_llm: bool = True) -> None:
        """`cliente` permite inyectar un Ollama falso en los tests."""
        self.catalogo = catalogo
        self._cliente = cliente
        if cliente is None and usar_llm and ollama is not None:
            self._cliente = ollama.Client(host=LLM.host, timeout=LLM.timeout_s)
        self.llm_disponible = self._cliente is not None
        self.ultimo_origen = "reglas"          # "reglas" o "llm" (para el registro de tiempos)

    # --- Arranque (no fatal: sin Ollama funcionan las reglas) --------------------

    def verificar(self) -> bool:
        """True si Ollama responde y tiene el modelo. Si no, avisa y se usan solo reglas."""
        if self._cliente is None:
            log.warning("Ollama no está disponible: solo se usarán las reglas (sin modelo de respaldo).")
            return False
        try:
            modelos = self._cliente.list()
            nombres = set()
            for m in (modelos["models"] if isinstance(modelos, dict) else getattr(modelos, "models", [])):
                for clave in ("model", "name"):
                    valor = m.get(clave) if isinstance(m, dict) else getattr(m, clave, None)
                    if valor:
                        nombres.add(valor)
        except Exception as e:
            log.warning("No se pudo contactar con Ollama (%s): solo reglas.", e)
            self.llm_disponible = False
            return False
        buscado = {LLM.modelo} if ":" in LLM.modelo else {LLM.modelo, f"{LLM.modelo}:latest"}
        if not buscado & nombres:
            log.warning("El modelo '%s' no está descargado (ollama pull %s): solo reglas.", LLM.modelo, LLM.modelo)
            self.llm_disponible = False
            return False
        return True

    def precargar(self) -> None:
        """Carga el modelo en memoria para que la primera frase rara no tarde."""
        if not self.llm_disponible:
            return
        t0 = time.perf_counter()
        try:
            self._cliente.generate(model=LLM.modelo, prompt="", keep_alive=LLM.keep_alive)
            log.info("Modelo %s cargado en %.1f s", LLM.modelo, time.perf_counter() - t0)
        except Exception as e:
            log.warning("No se pudo precargar el modelo: %s", e)

    # --- Interpretación ----------------------------------------------------------

    def interpretar(self, texto: str, pendiente: Comando | None = None) -> Comando:
        """
        Frase -> Comando. Si hay un comando ambiguo pendiente, la frase es la
        respuesta a su pregunta y se fusionan. Nunca lanza excepciones por culpa
        del modelo: si falla, devuelve un comando desconocido.
        """
        texto = (texto or "").strip()
        if not texto:
            return pendiente if pendiente is not None else Comando.desconocido()

        comando = analizar(texto, self.catalogo, pendiente)
        if comando is not None:
            self.ultimo_origen = "reglas"
            return comando

        self.ultimo_origen = "llm"
        hallazgos = self._consultar_llm(texto)
        if hallazgos is None or not hallazgos.reconocido:
            return Comando.desconocido()
        return self._confirmar(completar(fundamentar(hallazgos, texto, self.catalogo), self.catalogo))

    def _confirmar(self, c: Comando) -> Comando:
        """
        Lo que supone el modelo se confirma antes de ejecutarlo: un modelo de
        1.5B puede entender "cierra la puerta" como "apaga el guardián". Las
        consultas (que no cambian nada) y las preguntas no necesitan confirmación.
        """
        if not c.ejecutable or c.accion == "consultar":
            return c
        return replace(c, es_ambiguo=True, propuesta=c,
                       pregunta_aclaracion=f"¿Quieres que {describir(c, self.catalogo)}?")

    def _consultar_llm(self, texto: str) -> Hallazgos | None:
        if not self.llm_disponible or self._cliente is None:
            return None
        t0 = time.perf_counter()
        try:
            respuesta = self._cliente.chat(
                model=LLM.modelo,
                messages=[{"role": "system", "content": PROMPT_SISTEMA}, {"role": "user", "content": texto}],
                format=ESQUEMA,
                options={"temperature": LLM.temperatura, "top_p": LLM.top_p, "num_predict": LLM.max_tokens},
                keep_alive=LLM.keep_alive,
            )
            mensaje = respuesta["message"] if isinstance(respuesta, dict) else respuesta.message
            contenido = mensaje["content"] if isinstance(mensaje, dict) else mensaje.content
            datos = json.loads(contenido)
            if not isinstance(datos, dict):
                raise ValueError("el modelo no devolvió un objeto JSON")
        except Exception as e:   # red, timeout, JSON roto...: se degrada a "no entendí"
            log.warning("El modelo de respaldo falló (%s): %s", type(e).__name__, e)
            return None
        log.debug("LLM (%.2f s): %s", time.perf_counter() - t0, datos)
        return _a_hallazgos(datos)
