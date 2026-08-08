"""Troceado consciente de la estructura del documento.

Por qué no un splitter de tamaño fijo: un protocolo postoperatorio está escrito
en secciones ("Cuidado de la herida", "Signos de alarma", "Medicación"), y cada
sección es una unidad de sentido. Cortar a ciegas cada 800 caracteres parte
"acuda a urgencias si presenta fiebre superior a" de "38.5 grados", y entonces
ni el denso ni el léxico recuperan la regla completa.

Además cada trozo guarda su encabezado, que es lo que permite al agente citar en
voz: «según el protocolo de alta, sección signos de alarma…».
"""

import re
from dataclasses import dataclass

# ~800 tokens de contexto por trozo. Se cuenta en caracteres (≈4 por token en
# español) para no cargar un tokenizador solo para esto.
MAX_CHARS = 1400
MIN_CHARS = 120          # por debajo de esto, el trozo se funde con el vecino
OVERLAP_CHARS = 150      # arrastre entre trozos de una misma sección

_ENCABEZADO = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_FIN_FRASE = re.compile(r"(?<=[.!?:;])\s+")


@dataclass(slots=True)
class Trozo:
    ordinal: int
    content: str
    heading: str | None
    page: int | None = None

    @property
    def n_chars(self) -> int:
        return len(self.content)


def _secciones(markdown: str) -> list[tuple[str | None, str]]:
    """Parte el markdown por encabezados, conservando la jerarquía como ruta.

    Un H3 bajo un H2 se etiqueta "H2 › H3", de modo que la cita sea navegable
    aunque el encabezado hoja sea genérico ("Advertencias").
    """
    marcas = list(_ENCABEZADO.finditer(markdown))
    if not marcas:
        cuerpo = markdown.strip()
        return [(None, cuerpo)] if cuerpo else []

    fuera = []
    if marcas[0].start() > 0:
        preambulo = markdown[: marcas[0].start()].strip()
        if preambulo:
            fuera.append((None, preambulo))

    pila: list[tuple[int, str]] = []
    for i, m in enumerate(marcas):
        nivel, titulo = len(m.group(1)), m.group(2).strip()
        while pila and pila[-1][0] >= nivel:
            pila.pop()
        pila.append((nivel, titulo))
        ruta = " › ".join(t for _, t in pila)

        fin = marcas[i + 1].start() if i + 1 < len(marcas) else len(markdown)
        cuerpo = markdown[m.end() : fin].strip()
        if cuerpo:
            fuera.append((ruta, cuerpo))
    return fuera


def _partir_largo(texto: str) -> list[str]:
    """Parte una sección que excede MAX_CHARS por frases, con solape.

    El solape existe para que una regla que cae justo en la costura siga
    completa en al menos uno de los dos trozos.
    """
    frases = _FIN_FRASE.split(texto)
    piezas: list[str] = []
    actual = ""

    for frase in frases:
        if actual and len(actual) + len(frase) + 1 > MAX_CHARS:
            piezas.append(actual.strip())
            cola = actual[-OVERLAP_CHARS:]
            # Arranca el solape en frontera de palabra, no a mitad de una.
            corte = cola.find(" ")
            actual = (cola[corte + 1 :] if corte != -1 else "") + " " + frase
        else:
            actual = f"{actual} {frase}".strip() if actual else frase

    if actual.strip():
        piezas.append(actual.strip())
    return piezas


def _es_descendiente(hijo: str | None, padre: str | None) -> bool:
    """¿`hijo` cuelga de `padre` en la jerarquía de encabezados?"""
    if not hijo or not padre:
        return False
    return hijo == padre or hijo.startswith(f"{padre} › ")


def trocear(markdown: str) -> list[Trozo]:
    """Markdown estructurado (salida de Docling) -> trozos listos para embeber.

    Regla para secciones cortas: sólo se funden con un PARIENTE en la jerarquía
    (padre o hijo), nunca con un hermano.

      - Hacia atrás, con su sección padre: un «### Cuándo llamar al equipo» de
        dos líneas se pega a su «## Signos de alarma».
      - Hacia adelante, si el trozo corto es ANTECESOR del siguiente: el
        preámbulo bajo el título del documento se pega a su primera sección.

    Fundir con un hermano cruzaría una frontera semántica: ese mismo «Cuándo
    llamar al equipo» acabaría etiquetado «Medicación» sólo por ser la sección
    que viene después. En un protocolo clínico eso archiva una regla de alarma
    bajo el epígrafe equivocado, y el agente la citaría mal por voz.

    Si no hay pariente, el trozo corto se emite suelto: más vale uno pequeño
    bien etiquetado que uno grande mal etiquetado.
    """
    trozos: list[Trozo] = []
    pendiente: tuple[str | None, str] | None = None  # corto aún sin sitio

    def _emitir(heading: str | None, cuerpo: str) -> None:
        for pieza in _partir_largo(cuerpo) if len(cuerpo) > MAX_CHARS else [cuerpo]:
            trozos.append(Trozo(ordinal=len(trozos), content=pieza, heading=heading))

    for heading, cuerpo in _secciones(markdown):
        if not cuerpo.strip():
            continue

        # ¿Hay un corto esperando que sea antecesor de esta sección?
        if pendiente is not None:
            h_prev, texto_prev = pendiente
            if _es_descendiente(heading, h_prev):
                # Preámbulo -> se antepone; manda el encabezado del hijo, que es
                # de quien es el grueso del contenido.
                cuerpo = f"{texto_prev}\n\n{cuerpo}"
            else:
                _emitir(h_prev, texto_prev)   # sin pariente: suelto
            pendiente = None

        if len(cuerpo) < MIN_CHARS:
            if trozos and _es_descendiente(heading, trozos[-1].heading):
                # Hacia atrás. El subencabezado se pierde como etiqueta pero se
                # conserva DENTRO del texto: sigue siendo buscable y el LLM lo ve.
                sub = heading.split(" › ")[-1] if heading else None
                extra = f"{sub}\n{cuerpo}" if sub else cuerpo
                trozos[-1].content = f"{trozos[-1].content}\n\n{extra}"
            else:
                pendiente = (heading, cuerpo)
            continue

        _emitir(heading, cuerpo)

    if pendiente is not None:
        h, texto = pendiente
        if trozos:
            trozos[-1].content = f"{trozos[-1].content}\n\n{texto}"
        else:
            _emitir(h, texto)

    return trozos


def texto_a_embeber(trozo: Trozo) -> str:
    """Antepone el encabezado al contenido antes de embeber.

    Un trozo que dice "Mantenerla seca 48 horas" es ambiguo suelto; con
    "Cuidado de la herida › Mantenerla seca 48 horas" el vector queda mucho
    mejor situado. El encabezado se guarda además aparte, para la cita.
    """
    return f"{trozo.heading}\n\n{trozo.content}" if trozo.heading else trozo.content
