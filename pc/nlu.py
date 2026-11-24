"""
nlu.py — Comprensión de órdenes por REGLAS (sin modelo de lenguaje).

Un modelo de 1.5B tarda ~0.4 s y a veces inventa datos; unas reglas bien
hechas resuelven el 95 % de las órdenes de una casa en menos de 1 ms y nunca
inventan. El modelo (entendedor.py) solo entra cuando las reglas no reconocen
nada.

Pasos:
  extraer()   texto -> Hallazgos (qué verbo, objeto, zonas, canción... aparecen)
  completar() Hallazgos -> Comando (decide si falta algo y qué preguntar)
  fusionar()  comando ambiguo pendiente + respuesta del usuario -> Comando

Comando (lo que ejecuta acciones.py):
  accion    encender | apagar | reproducir | detener | subir | bajar | fijar |
            consultar | desconocido
  objetivo  luces | ventilador | guardian | alarma | musica | volumen |
            temperatura | humedad | estado | todo
  zonas     claves de config.ZONAS_CASA (para luces)
  cancion   número de pista del SD         aleatoria  elegir una al azar
  valor     volumen 0..VOLUMEN_MAX (para "fijar")
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from catalogo import Catalogo, normalizar
from config import VOLUMEN_MAX, ZONAS_CASA

OBJETIVOS = ("luces", "ventilador", "guardian", "alarma", "musica", "volumen",
             "temperatura", "humedad", "estado", "todo")
ACCIONES = ("encender", "apagar", "reproducir", "detener", "subir", "bajar", "fijar", "consultar", "desconocido")


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Comando:
    accion: str = "desconocido"
    objetivo: str | None = None
    zonas: tuple[str, ...] = ()
    cancion: int | None = None
    aleatoria: bool = False
    valor: int | None = None
    es_ambiguo: bool = False
    pregunta_aclaracion: str | None = None
    nota: str | None = None            # frase para decir cuando no se pudo interpretar
    # Comando que el modelo de respaldo "supone" y que se ejecuta solo si el
    # usuario responde que sí ("¿Quieres que apague el ventilador?").
    propuesta: Comando | None = None

    @property
    def ejecutable(self) -> bool:
        return not self.es_ambiguo and self.accion != "desconocido"

    def a_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def desconocido(cls, nota: str | None = None) -> Comando:
        return cls(nota=nota)


@dataclass(frozen=True)
class Hallazgos:
    """Lo que aparece en la frase, sin decidir todavía si es un comando completo."""

    accion: str | None = None
    objetivo: str | None = None
    zonas: tuple[str, ...] = ()
    cancion: int | None = None              # pista del SD
    cancion_fuera_de_rango: int | None = None
    aleatoria: bool = False
    valor: int | None = None
    varios_objetivos: bool = False          # "prende la sala y el ventilador"

    @property
    def reconocido(self) -> bool:
        """¿Hay algo para construir una orden? (si no, se pasa al LLM)"""
        return self.accion is not None or self.objetivo is not None

    @property
    def util(self) -> bool:
        """¿Aporta algo como respuesta a una aclaración?"""
        return (self.reconocido or bool(self.zonas) or self.cancion is not None
                or self.aleatoria or self.valor is not None or self.cancion_fuera_de_rango is not None)


# ---------------------------------------------------------------------------
# Patrones (sobre texto normalizado: minúsculas y sin tildes)
# ---------------------------------------------------------------------------

_ZONA = {
    "bano": r"\b(bano|banos|sanitario|wc)\b",
    "cocina": r"\bcocina\b",
    "comedor": r"\bcomedor\b",
    "sala": r"\b(sala|salon|living|estancia)\b",
    "cuarto": r"\b(cuarto|recamara|habitacion|dormitorio|alcoba)\b",
    "exterior": r"\b(exterior|afuera|fuera|fachada|patio|jardin|entrada|frente|enfrente|porche)\b",
}
_TODAS = re.compile(r"\b(todas|todos|toda la casa|toda)\b")
_TODO = re.compile(r"\btodo\b")

_ENCENDER = re.compile(r"\b(enciend\w*|encend\w*|prend\w*|activ\w*|inici\w*|conect\w*|ilumin\w*|alumbr\w*"
                       r"|pon|ponme|pone|poner|ponga|reproduc\w*|toca|tocame|escuch\w*)\b")
# "para" solo cuenta como verbo ante música ("para la música"): en "luz para la cocina" es preposición.
_APAGAR = re.compile(r"\b(apag\w*|desactiv\w*|desconect\w*|quit\w*|cort\w*|deten\w*|parar|pausa\w*|calla\w*|silenci\w*|stop"
                     r"|para(?= (?:la |el |esa |esta )?(?:musica|cancion|rola|tema|disco)\b))\b")

_GUARDIAN = re.compile(r"\b(guardian|vigilancia|vigila|modo seguridad|seguridad|alarma|sirena)\b")
_ALARMA = re.compile(r"\b(alarma|sirena)\b")
_VENTILADOR = re.compile(r"\b(ventilador|abanico|ventilacion)\b")
_LUCES = re.compile(r"\b(luz|luces|foco|focos|lampara|lamparas|iluminacion|ilumina\w*|alumbra\w*)\b")
_MUSICA = re.compile(r"\b(musica|cancion|canciones|tema|temas|rola|rolas|pista|disco|playlist|cancioncita|melodia)\b")
_VOLUMEN = re.compile(r"\bvolumen\b")
_SUBIR = re.compile(r"\b(sube|subir|subele|aumenta\w*|incrementa\w*|mas fuerte|mas alto|mas volumen)\b")
_BAJAR = re.compile(r"\b(baja|bajar|bajale|disminuye\w*|reduce\w*|mas bajo|mas suave|mas despacio|menos volumen)\b")
_ALEATORIA = re.compile(r"\b(cualquiera|cualquier|aleatori\w*|al azar|sorprende\w*|la que sea|la que quieras|lo que sea|alguna|algo)\b")
_CONSULTA = re.compile(r"\b(temperatura|grados|humedad|clima|como esta la casa|estado de la casa|estado)\b")
_PREGUNTA = re.compile(r"\b(cual|cuanta|cuanto|que|como|dime|dame|dime|hay|esta|estamos|dime)\b")

_NUMEROS = {"uno": 1, "una": 1, "primero": 1, "primera": 1, "primer": 1,
            "dos": 2, "segundo": 2, "segunda": 2, "tres": 3, "tercero": 3, "tercera": 3, "tercer": 3,
            "cuatro": 4, "cuarto": 4, "cuarta": 4, "cinco": 5, "quinto": 5, "quinta": 5,
            "seis": 6, "sexto": 6, "sexta": 6, "siete": 7, "septimo": 7, "septima": 7,
            "ocho": 8, "octavo": 8, "octava": 8, "nueve": 9, "noveno": 9, "novena": 9,
            "diez": 10, "decimo": 10, "decima": 10, "once": 11, "doce": 12}
_NUM = r"(\d{1,2}|" + "|".join(sorted(_NUMEROS, key=len, reverse=True)) + r")"
_CANCION_NUM = re.compile(rf"\b(?:cancion|pista|tema|rola|track|numero)\s+(?:numero\s+)?{_NUM}\b")
_NUM_SUELTO = re.compile(rf"\b{_NUM}\b")


def _numero(texto: str) -> int | None:
    return int(texto) if texto.isdigit() else _NUMEROS.get(texto)


def _primera_aparicion(patrones: dict[str, re.Pattern], plano: str) -> str | None:
    """La clave cuyo patrón aparece antes en el texto."""
    mejor, posicion = None, len(plano) + 1
    for clave, patron in patrones.items():
        m = patron.search(plano)
        if m and m.start() < posicion:
            mejor, posicion = clave, m.start()
    return mejor


def _verbo(plano: str) -> str | None:
    """'encender' o 'apagar' según el verbo que aparezca primero."""
    return _primera_aparicion({"encender": _ENCENDER, "apagar": _APAGAR}, plano)


def _zonas(plano: str) -> tuple[str, ...]:
    if _TODAS.search(plano):
        return tuple(ZONAS_CASA)
    return tuple(z for z in ZONAS_CASA if re.search(_ZONA[z], plano))


# ---------------------------------------------------------------------------
# 1. Extracción
# ---------------------------------------------------------------------------

def extraer(texto: str, catalogo: Catalogo, contexto: str | None = None) -> Hallazgos:
    """
    Busca en la frase todo lo que sea relevante.

    contexto: objetivo de la aclaración pendiente ("musica", "luces"...). En una
    respuesta a "¿qué canción?" basta con decir "la tres" o "la segunda".
    """
    plano = normalizar(texto)
    if not plano:
        return Hallazgos()

    verbo = _verbo(plano)
    zonas = _zonas(plano)
    cancion_texto = catalogo.buscar(plano)
    palabra_musica = bool(_MUSICA.search(plano))
    hay_volumen = bool(_VOLUMEN.search(plano))

    # --- Qué cosa se menciona (puede haber varias) ---
    candidatos: list[str] = []
    if _GUARDIAN.search(plano):
        candidatos.append("alarma" if (_ALARMA.search(plano) and verbo == "apagar") else "guardian")
    if _VENTILADOR.search(plano):
        candidatos.append("ventilador")
    if _LUCES.search(plano):
        candidatos.append("luces")
    if hay_volumen or ((_SUBIR.search(plano) or _BAJAR.search(plano)) and not candidatos and not zonas):
        candidatos.append("volumen")
    elif palabra_musica or (cancion_texto is not None and not candidatos and not zonas):
        # Un título solo cuenta si no hay otro objeto: "prende dos luces" no pide la canción "Dos".
        candidatos.append("musica")
    consulta = _CONSULTA.search(plano)
    if consulta and (_PREGUNTA.search(plano) or plano.startswith(("temperatura", "humedad"))) and not candidatos:
        palabra = consulta.group(1)
        candidatos.append("humedad" if palabra == "humedad" else "temperatura" if palabra in ("temperatura", "grados")
                          else "estado")
    if not candidatos and _TODO.search(plano) and verbo:
        candidatos.append("todo")
    if not candidatos and zonas and verbo:
        candidatos.append("luces")        # "prende la sala"
    if not candidatos and contexto == "musica" and _ALEATORIA.search(plano):
        candidatos.append("musica")

    objetivo = candidatos[0] if candidatos else None
    varios = len(set(candidatos)) > 1
    # "prende la sala y el ventilador": una zona unida con "y" a otro aparato son dos órdenes.
    # ("el ventilador de la sala", sin "y", es una sola: la zona se ignora).
    if objetivo in ("ventilador", "guardian", "musica") and zonas and verbo and re.search(r"\by\b", plano):
        varios = True
    accion: str | None = verbo

    # --- Música y volumen ---
    cancion = cancion_texto.pista if cancion_texto else None
    fuera_de_rango = None
    aleatoria = False
    valor = None

    if objetivo == "musica" or (contexto == "musica" and objetivo is None):
        # Número de canción: con la palabra "canción/pista", o suelto si se está aclarando.
        if cancion is None:
            m = _CANCION_NUM.search(plano) or (_NUM_SUELTO.search(plano) if contexto == "musica" else None)
            if m:
                numero = _numero(m.group(1))
                elegida = catalogo.por_numero(numero) if numero else None
                if elegida is not None:
                    cancion = elegida.pista
                elif numero is not None:
                    fuera_de_rango = numero
        aleatoria = bool(_ALEATORIA.search(plano)) and cancion is None and fuera_de_rango is None

    if contexto == "volumen" and objetivo is None:
        # Respuesta a "¿a qué volumen?": basta con decir "50" o "al máximo".
        valor = _valor_volumen(plano, aunque_no_diga_volumen=True)
        if valor is not None:
            accion = "fijar"
    elif objetivo == "volumen" or (objetivo == "musica" and (_SUBIR.search(plano) or _BAJAR.search(plano))):
        objetivo = "volumen"
        valor = _valor_volumen(plano)
        if valor is not None:
            accion = "fijar"
        elif _SUBIR.search(plano):
            accion = "subir"
        elif _BAJAR.search(plano):
            accion = "bajar"
        else:
            accion = None
    elif objetivo == "musica":
        # "para la música", "deja de sonar" -> detener; el resto -> reproducir
        accion = "detener" if (verbo == "apagar" and cancion is None) else "reproducir"
    elif objetivo in ("temperatura", "humedad", "estado"):
        accion = "consultar"

    return Hallazgos(accion=accion, objetivo=objetivo, zonas=zonas if objetivo in (None, "luces", "todo") else (),
                     cancion=cancion, cancion_fuera_de_rango=fuera_de_rango, aleatoria=aleatoria,
                     valor=valor, varios_objetivos=varios)


def _valor_volumen(plano: str, aunque_no_diga_volumen: bool = False) -> int | None:
    """Volumen pedido como porcentaje ('al 50') o con palabras ('al máximo'), en la escala 0..VOLUMEN_MAX."""
    dice_volumen = bool(_VOLUMEN.search(plano)) or aunque_no_diga_volumen
    if re.search(r"\b(maximo|al tope|a tope|todo el volumen)\b", plano):
        return VOLUMEN_MAX
    if re.search(r"\b(minimo|silencio|mudo)\b", plano) and dice_volumen:
        return 0
    if re.search(r"\bmedio\b", plano):
        return VOLUMEN_MAX // 2
    m = re.search(r"\b(\d{1,3})\b", plano)
    if m and dice_volumen:
        return min(VOLUMEN_MAX, round(min(int(m.group(1)), 100) * VOLUMEN_MAX / 100))
    return None


# ---------------------------------------------------------------------------
# 2. Completar: ¿falta algo?
# ---------------------------------------------------------------------------

def nombres_zonas() -> str:
    """'baño, cocina, comedor, sala, cuarto, exterior o todas'"""
    return ", ".join(z[2] for z in ZONAS_CASA.values()) + " o todas"


def _preguntar(c: Comando, pregunta: str) -> Comando:
    return replace(c, es_ambiguo=True, pregunta_aclaracion=pregunta)


def completar(h: Hallazgos, catalogo: Catalogo) -> Comando:
    """Convierte los hallazgos en un Comando; si falta información, es ambiguo y trae la pregunta."""
    if h.varios_objetivos:
        return Comando.desconocido("Dime una orden a la vez, por favor.")

    objetivo, accion = h.objetivo, h.accion
    if objetivo is None and h.zonas and accion in ("encender", "apagar"):
        objetivo = "luces"
    if objetivo is None:
        return Comando.desconocido()

    base = Comando(accion or "desconocido", objetivo)

    if objetivo == "alarma":
        return Comando("apagar", "alarma")
    if objetivo == "guardian":
        return Comando(accion or "encender", "guardian")

    if objetivo in ("temperatura", "humedad", "estado"):
        return Comando("consultar", objetivo)

    if objetivo == "ventilador":
        if accion is None:
            return _preguntar(base, "¿Quieres encender o apagar el ventilador?")
        return Comando(accion, "ventilador")

    if objetivo == "todo":
        if accion is None:
            return _preguntar(base, "¿Quieres encender o apagar todo?")
        return Comando(accion, "todo")

    if objetivo == "luces":
        base = replace(base, zonas=h.zonas)
        if accion is None:
            lugar = "las luces" if not h.zonas else "las luces de " + " y ".join(ZONAS_CASA[z][1] for z in h.zonas)
            return _preguntar(base, f"¿Quieres encender o apagar {lugar}?")
        if not h.zonas:
            return _preguntar(base, f"¿Qué luces quieres {accion}: {nombres_zonas()}?")
        return replace(base, accion=accion)

    if objetivo == "musica":
        if accion == "detener":
            return Comando("detener", "musica")
        if h.cancion_fuera_de_rango is not None:
            return _preguntar(base, f"Solo tengo {len(catalogo)} canciones. ¿Cuál quieres escuchar?"
                              if len(catalogo) else "No tengo canciones cargadas.")
        if h.cancion is not None or h.aleatoria:
            return Comando("reproducir", "musica", cancion=h.cancion, aleatoria=h.aleatoria)
        if len(catalogo) == 0:
            return Comando.desconocido("No tengo canciones cargadas.")
        return _preguntar(Comando("reproducir", "musica"),
                          f"¿Qué canción quieres escuchar? Tengo {catalogo.titulos_para_voz()}.")

    if objetivo == "volumen":
        if accion == "fijar" and h.valor is not None:
            return Comando("fijar", "volumen", valor=h.valor)
        if accion in ("subir", "bajar"):
            return Comando(accion, "volumen")
        return _preguntar(Comando("fijar", "volumen"), "¿A qué volumen? Dime un número del 0 al 100.")

    return Comando.desconocido()


# ---------------------------------------------------------------------------
# 3. Aclaraciones
# ---------------------------------------------------------------------------

def fusionar(pendiente: Comando, h: Hallazgos, catalogo: Catalogo) -> Comando:
    """
    Combina el comando ambiguo pendiente con la respuesta del usuario.

    Lo que el usuario dice ahora tiene prioridad ("no, mejor el ventilador" cambia
    el objetivo); lo que no dice se toma del pendiente. Si la respuesta no aporta
    nada, se devuelve el pendiente sin cambios.
    """
    if not h.util:
        return pendiente

    objetivo = h.objetivo or pendiente.objetivo
    cambio_de_objetivo = h.objetivo is not None and h.objetivo != pendiente.objetivo
    accion = h.accion or pendiente.accion
    if accion == "desconocido":
        accion = None
    if h.valor is not None and objetivo == "volumen":
        accion = "fijar"

    zonas = h.zonas or (() if cambio_de_objetivo else pendiente.zonas)
    cancion = h.cancion if h.cancion is not None else (None if cambio_de_objetivo else pendiente.cancion)
    fusionado = Hallazgos(
        accion=accion, objetivo=objetivo, zonas=zonas, cancion=cancion,
        cancion_fuera_de_rango=h.cancion_fuera_de_rango,
        aleatoria=h.aleatoria or (pendiente.aleatoria and not cambio_de_objetivo),
        valor=h.valor if h.valor is not None else (None if cambio_de_objetivo else pendiente.valor),
    )
    return completar(fusionado, catalogo)


_SI = re.compile(r"\b(si|sip|claro|dale|ok|okey|vale|correcto|afirmativo|exacto|hazlo|adelante|por favor)\b")
_NO = re.compile(r"\b(no|nel|nop|negativo|nunca|cancela\w*|olvida\w*)\b")


def describir(c: Comando, catalogo: Catalogo) -> str:
    """Frase subjuntiva para confirmar: 'apague el ventilador' (tras '¿Quieres que ...?')."""
    on = c.accion == "encender"
    if c.objetivo == "luces":
        if len(c.zonas) == len(ZONAS_CASA):
            return f"{'encienda' if on else 'apague'} todas las luces"
        nombres = [ZONAS_CASA[z][1] for z in c.zonas]
        lugar = " y ".join(nombres)
        lugar = "del " + lugar[3:] if lugar.startswith("el ") else "de " + lugar   # "de el baño" -> "del baño"
        return f"{'encienda' if on else 'apague'} las luces {lugar}"
    if c.objetivo == "ventilador":
        return f"{'encienda' if on else 'apague'} el ventilador"
    if c.objetivo == "todo":
        return f"{'encienda' if on else 'apague'} todo"
    if c.objetivo == "guardian":
        return f"{'active' if on else 'desactive'} el modo guardián"
    if c.objetivo == "alarma":
        return "apague la alarma"
    if c.objetivo == "musica":
        if c.accion == "detener":
            return "detenga la música"
        cancion = catalogo.por_pista(c.cancion) if c.cancion is not None else None
        return f"reproduzca {cancion.titulo}" if cancion else "reproduzca una canción al azar"
    if c.objetivo == "volumen":
        if c.accion == "fijar":
            return f"ponga el volumen al {round((c.valor or 0) * 100 / VOLUMEN_MAX)} por ciento"
        return f"{'suba' if c.accion == 'subir' else 'baje'} el volumen"
    return "haga eso"


def analizar(texto: str, catalogo: Catalogo, pendiente: Comando | None = None) -> Comando | None:
    """
    Interpreta la frase solo con reglas. Devuelve None si no reconoce nada
    (para que el LLM lo intente).
    """
    if pendiente is not None and pendiente.propuesta is not None:
        # Se está pidiendo confirmación de lo que supuso el modelo: sí / no / otra orden.
        plano = normalizar(texto)
        if _NO.search(plano) and not _SI.search(plano):
            return Comando.desconocido("De acuerdo, no hago nada.")
        if _SI.search(plano) and not _NO.search(plano):
            return pendiente.propuesta
        nuevo = analizar(texto, catalogo, None)
        return nuevo if nuevo is not None else pendiente   # no la entendí: se repite la pregunta
    if pendiente is not None and pendiente.es_ambiguo:
        h = extraer(texto, catalogo, contexto=pendiente.objetivo)
        return fusionar(pendiente, h, catalogo)   # sin hallazgos devuelve el pendiente (sin progreso)
    h = extraer(texto, catalogo)
    if not h.reconocido:
        return None
    return completar(h, catalogo)
