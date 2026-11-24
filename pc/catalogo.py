"""
catalogo.py — Lista de canciones de la casa (pc/canciones.json).

Cada canción tiene el número de archivo de la tarjeta SD (`pista`), un título
y alias. El usuario puede pedirla por:
  - número de orden en la lista:  "la canción tres", "la segunda"
  - título o alias:               "pon Thriller", "la de los Beatles"
  - o dejar que se elija una al azar ("cualquiera", "sorpréndeme").

El reconocimiento por título es tolerante a errores de transcripción: una
palabra del título que Whisper escribió casi igual ("trailer" por "thriller")
cuenta como coincidencia.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from config import ARCHIVO_CANCIONES, PISTA_ALARMA, PISTA_MIN_CANCION

log = logging.getLogger("catalogo")

PISTA_MAX = 30
_RUTA_POR_DEFECTO = Path(__file__).resolve().parent / ARCHIVO_CANCIONES

# Palabras que no ayudan a identificar una canción.
_RELLENO = {
    "el", "la", "los", "las", "un", "una", "de", "del", "al", "y", "a", "en", "con", "por", "para",
    "mi", "tu", "su", "que", "se", "lo", "me", "te", "es", "no",
    # Palabras genéricas de música: "Canción de cuna" se reconoce por "cuna", no por "canción".
    "cancion", "canciones", "musica", "tema", "pista", "rola", "disco",
}


class ErrorCatalogo(Exception):
    """El archivo de canciones no existe o tiene un formato inválido."""


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes ni signos y con espacios simples ('¡Canción Dos!' -> 'cancion dos')."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes).split())


@dataclass(frozen=True)
class Cancion:
    pista: int
    titulo: str
    alias: tuple[str, ...] = ()

    @property
    def nombres(self) -> list[str]:
        """Título y alias normalizados, de más largo a más corto."""
        todos = {normalizar(n) for n in (self.titulo, *self.alias) if normalizar(n)}
        return sorted(todos, key=len, reverse=True)


def _palabras_clave(nombre: str) -> list[str]:
    return [p for p in nombre.split() if p not in _RELLENO]


class Catalogo:

    def __init__(self, canciones: list[Cancion]) -> None:
        pistas = [c.pista for c in canciones]
        if len(set(pistas)) != len(pistas):
            raise ErrorCatalogo("Hay dos canciones con el mismo número de pista.")
        for c in canciones:
            if not PISTA_MIN_CANCION <= c.pista <= PISTA_MAX:
                raise ErrorCatalogo(
                    f"Pista {c.pista} ('{c.titulo}') fuera de rango: usa {PISTA_MIN_CANCION} a {PISTA_MAX} "
                    f"(la {PISTA_ALARMA} es la alarma)."
                )
            if not c.titulo.strip():
                raise ErrorCatalogo(f"La pista {c.pista} no tiene título.")
        self.canciones = list(canciones)

    def __len__(self) -> int:
        return len(self.canciones)

    @classmethod
    def cargar(cls, ruta: str | Path | None = None) -> Catalogo:
        """Lee canciones.json. Un archivo ausente da un catálogo vacío (con aviso)."""
        ruta = Path(ruta) if ruta else _RUTA_POR_DEFECTO
        if not ruta.exists():
            log.warning("No existe %s: no habrá canciones.", ruta)
            return cls([])
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            canciones = [
                Cancion(int(c["pista"]), str(c["titulo"]).strip(), tuple(str(a) for a in c.get("alias", [])))
                for c in datos["canciones"]
            ]
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ErrorCatalogo(f"No se pudo leer {ruta.name}: {e!r}") from e
        return cls(canciones)

    # --- Búsqueda -----------------------------------------------------------

    def por_numero(self, numero: int) -> Cancion | None:
        """La canción número `numero` de la lista (empezando en 1)."""
        return self.canciones[numero - 1] if 1 <= numero <= len(self.canciones) else None

    def por_pista(self, pista: int) -> Cancion | None:
        return next((c for c in self.canciones if c.pista == pista), None)

    def buscar(self, texto: str) -> Cancion | None:
        """
        Canción mencionada en `texto` (por título o alias), o None si no hay
        ninguna o hay empate entre varias.
        """
        plano = normalizar(texto)
        palabras_texto = plano.split()
        if not palabras_texto:
            return None

        mejores: dict[int, float] = {}
        for cancion in self.canciones:
            mejores[cancion.pista] = max((self._puntuar(n, plano, palabras_texto) for n in cancion.nombres), default=0.0)

        if not mejores:
            return None
        maximo = max(mejores.values())
        if maximo < 0.66:
            return None
        ganadoras = [p for p, s in mejores.items() if s == maximo]
        return self.por_pista(ganadoras[0]) if len(ganadoras) == 1 else None

    @staticmethod
    def _puntuar(nombre: str, plano: str, palabras_texto: list[str]) -> float:
        """1.0 si el nombre aparece completo; si no, la fracción de sus palabras clave que aparecen."""
        if re.search(rf"\b{re.escape(nombre)}\b", plano):
            return 1.0
        claves = _palabras_clave(nombre)
        if not claves:
            return 0.0
        aciertos = 0
        larga_acertada = False
        for clave in claves:
            # Palabras largas toleran más error de transcripción ("trailer" por "thriller").
            umbral = 0.78 if len(clave) >= 6 else 0.85
            if clave in palabras_texto or any(
                len(clave) >= 5 and difflib.SequenceMatcher(None, clave, p).ratio() >= umbral for p in palabras_texto
            ):
                aciertos += 1
                larga_acertada = larga_acertada or len(clave) >= 6
        if not aciertos:
            return 0.0
        fraccion = aciertos / len(claves)
        # Una palabra larga y distintiva basta para identificar ("bohemian" -> "Bohemian Rhapsody");
        # una corta o común ("luna" en "Luna llena"/"Luna nueva") no.
        return max(fraccion, 0.7) if larga_acertada else fraccion

    def titulos_para_voz(self, maximo: int = 5) -> str:
        """'Canción uno, Canción dos y 8 más' para decirlo en voz alta."""
        titulos = [c.titulo for c in self.canciones[:maximo]]
        resto = len(self.canciones) - len(titulos)
        if resto > 0:
            titulos.append(f"{resto} más")
        if len(titulos) <= 1:
            return "".join(titulos)
        return ", ".join(titulos[:-1]) + " y " + titulos[-1]
