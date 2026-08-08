"""Archivo -> Markdown con la jerarquía de encabezados intacta.

Esto es la pieza de la que cuelga todo lo demás. `chunking.trocear()` parte por
encabezados y construye rutas «Signos de alarma › Cuándo llamar al equipo» para
que el agente pueda decir por voz «según el protocolo, en la sección de signos de
alarma…». Un parser que devuelva un muro de texto plano no rompe el troceado de
forma visible: lo degrada a un splitter por frases y las citas dejan de existir.
En contexto clínico eso es lo que separa un sistema defendible de uno que suena
convincente y no se puede auditar.

MOTOR PRIMARIO PARA PDF: PyMuPDF. El plan de la Fase 0 daba por hecho lo
contrario («Docling conserva la jerarquía»). La medición dice que no. Sobre
`eval/corpus_prueba/*.pdf` en este MacBook Air M4, con los modelos ya
descargados (`scripts/spikes/spike_parsing.py`):

| Motor   | s/doc  | Arranque | RSS pico | Encabezados | Niveles correctos |
|---------|-------:|---------:|---------:|------------:|------------------:|
| PyMuPDF | 0.151 s|   0.25 s |    61 MB |       25/25 |         **25/25** |
| Docling | 0.494 s|  12.32 s |  1212 MB |       25/25 |          **3/25** |

El número que decide es 25/25 contra 3/25, y no es la velocidad ni la memoria.
Los dos motores ENCUENTRAN los mismos encabezados; Docling los emite todos como
`##`, sin excepción. Y una jerarquía plana no degrada el troceado, lo desactiva:
`chunking._secciones()` construye la ruta apilando niveles, así que con todos los
encabezados al mismo nivel la pila se vacía en cada sección y no se genera ni una
sola ruta «Signos de alarma › Cuándo llamar al equipo». Las citas del agente
desaparecen — justo lo que Docling estaba puesto a defender. (Los 3/25 son los
títulos de documento, que aciertan por ser la raíz.)

Los 1212 MB y los 12.32 s de arranque solo confirman la elección: el worker de
ingesta corre AL LADO del pipeline de voz en una máquina de 16 GB compartidos con
Whisper, bge-m3 y el reranker. La primera ejecución además tarda 49 s porque
descarga los modelos de layout.

Docling se queda como RESPALDO automático, y no por cortesía: cubre el único caso
que PyMuPDF no puede cubrir — un PDF escaneado sin capa de texto, donde la
extracción directa devuelve cadena vacía y hace falta OCR. Por eso el respaldo no
salta solo ante excepción, sino también cuando el primario no saca texto.

En .docx la conclusión se invierte y por eso Docling es ahí el primario: lee el
nivel del estilo del párrafo en vez de inferirlo, y devuelve `##`/`###`/`####`
bien anidados. PyMuPDF no abre .docx, así que el respaldo es un lector OOXML
propio (`docx_minimo`).
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

# Un encabezado es una línea corta: un párrafo entero en cuerpo grande es una
# cita destacada, no una sección. Sin este tope, un PDF con la introducción en
# 12 pt genera un encabezado de 400 caracteres y arrastra medio documento.
MAX_CHARS_ENCABEZADO = 110

# Diferencia mínima sobre el cuerpo para considerar que un tamaño es encabezado.
# 0.6 pt en vez de 0: los PDF exportados desde Word mezclan 9.96 y 10.0 en el
# mismo párrafo por redondeo del subsetting de fuentes.
DELTA_ENCABEZADO = 0.6

# Tope de descompresión al leer un .docx. Un .docx es un zip y un zip puede ser
# una bomba; el admin sube archivos de confianza pero el límite cuesta una línea.
MAX_BYTES_DOCX = 64 * 1024 * 1024

_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NIVEL_ESTILO = re.compile(r"^(?:Heading|Ttulo|Titulo|Título)\s*(\d)$", re.IGNORECASE)
_GUION_FINAL = re.compile(r"(\w)-$")


@dataclass(slots=True)
class Documento:
    """Resultado del parseo. `saltos` es lo que permite citar la página."""

    markdown: str
    motor: str
    paginas: int | None = None
    # Desplazamiento en caracteres del markdown donde empieza cada página.
    # Vacío para formatos sin paginación (docx, md, txt): ahí `page` es NULL y
    # la cita se queda en «archivo › sección», que sigue siendo navegable.
    saltos: list[int] = field(default_factory=list)

    @property
    def encabezados(self) -> int:
        return len(re.findall(r"^#{1,6}\s+\S", self.markdown, re.MULTILINE))


class SinTextoExtraible(RuntimeError):
    """El motor abrió el archivo pero no sacó texto: probablemente un escaneado."""


def pagina_de(doc: Documento, offset: int) -> int | None:
    """Página (1-based) a la que pertenece un desplazamiento del markdown."""
    if not doc.saltos:
        return None
    return bisect_right(doc.saltos, offset)


# ---------------------------------------------------------------------------
# PDF — PyMuPDF (primario)
# ---------------------------------------------------------------------------
def _tamano_cuerpo(paginas) -> float:
    """Tamaño de fuente del texto corrido, medido por volumen de caracteres.

    Se pesa por caracteres y no por número de líneas porque un documento con
    muchos encabezados cortos y pocos párrafos largos elegiría el tamaño del
    encabezado como cuerpo, y entonces no detectaría ninguna sección.
    """
    peso: dict[float, int] = {}
    for pagina in paginas:
        for bloque in pagina.get_text("dict")["blocks"]:
            for linea in bloque.get("lines", []):
                for tramo in linea["spans"]:
                    tam = round(tramo["size"] * 2) / 2      # rejilla de 0.5 pt
                    peso[tam] = peso.get(tam, 0) + len(tramo["text"].strip())
    if not peso:
        raise SinTextoExtraible("el PDF no tiene capa de texto")
    return max(peso.items(), key=lambda kv: kv[1])[0]


def _niveles(paginas, cuerpo: float) -> dict[float, int]:
    """Tamaño de fuente -> nivel de encabezado (1 = el mayor).

    Se ordena por tamaño en vez de fiarse de la negrita: la fila de cabecera de
    una tabla de dosis va en negrita al mismo tamaño que el cuerpo, y tomarla por
    encabezado partiría la tabla en secciones falsas — justo la tabla que hace
    falta entera para asociar «Cefalexina» con «500 mg».
    """
    candidatos: set[float] = set()
    for pagina in paginas:
        for bloque in pagina.get_text("dict")["blocks"]:
            for linea in bloque.get("lines", []):
                for tramo in linea["spans"]:
                    tam = round(tramo["size"] * 2) / 2
                    if tam >= cuerpo + DELTA_ENCABEZADO and tramo["text"].strip():
                        candidatos.add(tam)
    return {tam: n for n, tam in enumerate(sorted(candidatos, reverse=True)[:6], start=1)}


def normalizar(texto: str) -> str:
    """NFKC sobre todo lo que sale de un PDF. No es cosmética: es recuperación.

    Un PDF con tipografía de calidad guarda «fiebre» como «ﬁebre» — U+FB01, la
    ligadura fi, un carácter distinto de «f»+«i». Medido sobre este corpus,
    `"fiebre" in markdown` daba **False** en los tres protocolos: el índice léxico
    de Postgres nunca habría casado la palabra más importante del documento, y el
    embedding de un token roto queda mal situado. El agente respondería «no tengo
    esa información» a la pregunta que más importa acertar, y en la demo eso se
    lee como que el RAG no funciona.

    NFKC y no NFC porque es NFKC quien descompone las ligaduras; de paso unifica
    los espacios duros y los superíndices de las unidades.
    """
    return unicodedata.normalize("NFKC", texto)


def _texto_de_linea(linea: dict) -> tuple[str, float]:
    """Texto de la línea y el tamaño de fuente que domina en ella."""
    texto = normalizar("".join(t["text"] for t in linea["spans"]))
    peso: dict[float, int] = {}
    for tramo in linea["spans"]:
        tam = round(tramo["size"] * 2) / 2
        peso[tam] = peso.get(tam, 0) + len(tramo["text"].strip())
    dominante = max(peso.items(), key=lambda kv: kv[1])[0] if peso else 0.0
    return texto, dominante


def _unir_lineas(lineas: list[str]) -> str:
    """Reconstruye el párrafo deshaciendo los cortes de línea de la maquetación.

    Un PDF no guarda párrafos, guarda líneas colocadas. Si se unen con salto de
    línea, el markdown resultante convierte cada renglón en algo que el troceado
    trata como unidad; unidas con espacio vuelve a ser prosa. El guion final se
    deshace porque en español el corte silábico es frecuente en texto justificado
    y «apendicec-tomía» partido en dos no lo recupera ni el índice léxico.
    """
    salida = ""
    for linea in lineas:
        pieza = linea.strip()
        if not pieza:
            continue
        if not salida:
            salida = pieza
        elif _GUION_FINAL.search(salida) and pieza[:1].islower():
            salida = salida[:-1] + pieza
        else:
            salida = f"{salida} {pieza}"
    return salida


def _dentro(caja: tuple[float, float, float, float], recinto) -> bool:
    cx = (caja[0] + caja[2]) / 2
    cy = (caja[1] + caja[3]) / 2
    return recinto[0] <= cx <= recinto[2] and recinto[1] <= cy <= recinto[3]


def _elementos_de_pagina(pagina, niveles: dict[float, int]) -> list[str]:
    """Bloques de una página, en orden de lectura, ya convertidos a markdown."""
    try:
        tablas = pagina.find_tables().tables
    except Exception:  # noqa: BLE001 — find_tables es heurístico y puede reventar
        tablas = []

    piezas: list[tuple[float, str]] = []

    for tabla in tablas:
        try:
            md = normalizar(tabla.to_markdown()).strip()
        except Exception:  # noqa: BLE001
            continue
        if md:
            piezas.append((tabla.bbox[1], md))

    for bloque in pagina.get_text("dict")["blocks"]:
        if "lines" not in bloque:
            continue
        # Lo que cae dentro de una tabla ya se emitió como tabla; repetirlo
        # duplicaría las dosis en el corpus y el retrieval devolvería el mismo
        # fragmento dos veces.
        if any(_dentro(bloque["bbox"], t.bbox) for t in tablas):
            continue

        y = bloque["bbox"][1]
        acumulado: list[str] = []
        # Un encabezado largo se maqueta en varias líneas del mismo tamaño. Sin
        # acumularlas, «Protocolo de alta — Herniorrafia inguinal con / malla»
        # produce DOS secciones y la segunda se llama «malla»: el troceado le
        # cuelga contenido a una sección fantasma y la cita por voz queda absurda.
        encabezado: tuple[int, list[str]] | None = None

        for linea in bloque["lines"]:
            texto, tam = _texto_de_linea(linea)
            nivel = niveles.get(tam)
            es_encabezado = bool(
                nivel and texto.strip() and len(texto.strip()) <= MAX_CHARS_ENCABEZADO
            )

            # Otra línea del mismo encabezado: sigue siendo el mismo título.
            if es_encabezado and encabezado is not None and encabezado[0] == nivel:
                encabezado[1].append(texto)
                continue

            # Cualquier otra cosa lo cierra.
            if encabezado is not None:
                piezas.append((y, f"{'#' * encabezado[0]} {_unir_lineas(encabezado[1])}"))
                encabezado = None

            if es_encabezado:
                if acumulado:
                    piezas.append((y, _unir_lineas(acumulado)))
                    acumulado = []
                encabezado = (nivel, [texto])
            else:
                acumulado.append(texto)

        if encabezado is not None:
            piezas.append((y, f"{'#' * encabezado[0]} {_unir_lineas(encabezado[1])}"))
        if acumulado:
            piezas.append((y, _unir_lineas(acumulado)))

    piezas.sort(key=lambda p: p[0])
    return [texto for _, texto in piezas if texto.strip()]


def pdf_con_pymupdf(path: Path) -> Documento:
    """PDF -> markdown reconstruyendo los encabezados por tamaño de fuente.

    Se descartó fiarse de los marcadores del PDF (`doc.get_toc()`): un protocolo
    exportado desde Word los trae, uno escaneado o generado por un equipo de
    imagen no, y mantener dos caminos de los que solo uno se ejercita a diario es
    peor que uno solo que funciona siempre.
    """
    import pymupdf

    with pymupdf.open(path) as doc:
        paginas = list(doc)
        cuerpo = _tamano_cuerpo(paginas)
        niveles = _niveles(paginas, cuerpo)

        trozos: list[str] = []
        saltos: list[int] = []
        largo = 0
        for pagina in paginas:
            saltos.append(largo)
            for elemento in _elementos_de_pagina(pagina, niveles):
                trozos.append(elemento)
                largo += len(elemento) + 2

        markdown = "\n\n".join(trozos)
        if not markdown.strip():
            raise SinTextoExtraible("el PDF no tiene capa de texto")
        return Documento(markdown, "pymupdf", doc.page_count, saltos)


# ---------------------------------------------------------------------------
# PDF y DOCX — Docling (respaldo, y primario para .docx)
# ---------------------------------------------------------------------------
_convertidor = None


def _docling():
    """Convertidor único por proceso.

    Construirlo cuesta ~11 s porque carga los modelos de layout. Hacerlo por
    documento convertiría el respaldo en algo inservible justo cuando hace falta.
    """
    global _convertidor
    if _convertidor is None:
        from docling.document_converter import DocumentConverter

        _convertidor = DocumentConverter()
    return _convertidor


def con_docling(path: Path) -> Documento:
    resultado = _docling().convert(str(path))
    markdown = resultado.document.export_to_markdown()
    if not markdown.strip():
        raise SinTextoExtraible(f"docling no extrajo texto de {path.name}")
    paginas = getattr(resultado.document, "num_pages", None)
    if callable(paginas):
        paginas = paginas()
    # 0 significa "sin paginación" (un .docx no la tiene hasta que se maqueta),
    # no "cero páginas"; se guarda como NULL para no publicar un dato falso.
    if not isinstance(paginas, int) or paginas <= 0:
        paginas = None
    # Sin `saltos`: docling no expone el corte de página en el markdown exportado,
    # así que por este camino la cita se queda sin número de página.
    return Documento(markdown, "docling", paginas)


# ---------------------------------------------------------------------------
# DOCX — lector OOXML mínimo (respaldo de Docling)
# ---------------------------------------------------------------------------
def _texto_ooxml(nodo) -> str:
    return "".join(t.text or "" for t in nodo.iter(f"{_NS_W}t"))


def _nivel_ooxml(parrafo) -> int | None:
    props = parrafo.find(f"{_NS_W}pPr")
    if props is None:
        return None
    estilo = props.find(f"{_NS_W}pStyle")
    if estilo is None:
        return None
    m = _NIVEL_ESTILO.match((estilo.get(f"{_NS_W}val") or "").strip())
    return min(int(m.group(1)), 6) if m else None


def docx_minimo(path: Path) -> Documento:
    """.docx -> markdown leyendo el OOXML a pelo.

    Existe porque Docling es la única dependencia capaz de abrir .docx y dejar la
    ingesta atada a un solo motor sería un punto único de fallo para un formato
    que el administrador va a subir a diario. Un .docx es un zip con XML y los
    encabezados son un atributo del párrafo (`w:pStyle` = Heading1..6): esto no
    adivina nada, lee la etiqueta.
    """
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > MAX_BYTES_DOCX:
            raise ValueError("word/document.xml descomprimido excede el límite")
        xml = z.read("word/document.xml")

    cuerpo = ElementTree.fromstring(xml).find(f"{_NS_W}body")
    if cuerpo is None:
        raise SinTextoExtraible(f"{path.name} no tiene cuerpo de documento")

    piezas: list[str] = []
    for nodo in cuerpo:
        if nodo.tag == f"{_NS_W}p":
            texto = _texto_ooxml(nodo).strip()
            if not texto:
                continue
            nivel = _nivel_ooxml(nodo)
            piezas.append(f"{'#' * nivel} {texto}" if nivel else texto)
        elif nodo.tag == f"{_NS_W}tbl":
            filas = [
                [_texto_ooxml(celda).strip() for celda in fila.iter(f"{_NS_W}tc")]
                for fila in nodo.iter(f"{_NS_W}tr")
            ]
            if filas:
                piezas.append(_tabla_markdown(filas))

    markdown = "\n\n".join(piezas)
    if not markdown.strip():
        raise SinTextoExtraible(f"{path.name} no contiene texto")
    return Documento(markdown, "docx_minimo")


def _tabla_markdown(filas: list[list[str]]) -> str:
    ancho = max(len(f) for f in filas)

    def _fila(celdas: list[str]) -> str:
        return "| " + " | ".join(celdas + [""] * (ancho - len(celdas))) + " |"

    return "\n".join(
        [_fila(filas[0]), "|" + "---|" * ancho, *(_fila(f) for f in filas[1:])]
    )


# ---------------------------------------------------------------------------
# Texto plano y markdown
# ---------------------------------------------------------------------------
def texto_plano(path: Path) -> Documento:
    """.md y .txt entran tal cual: ya son la representación de destino.

    Un .txt sin almohadillas produce un único trozo sin encabezado, y está bien:
    el troceado lo parte por frases con solape. Inventar encabezados a partir de
    líneas en mayúsculas daría secciones falsas, que en un protocolo clínico es
    peor que no tener ninguna.
    """
    texto = path.read_text(encoding="utf-8", errors="replace")
    if not texto.strip():
        raise SinTextoExtraible(f"{path.name} está vacío")
    return Documento(texto, "texto")


# ---------------------------------------------------------------------------
# Enrutado
# ---------------------------------------------------------------------------
_POR_EXTENSION = {".pdf": "pdf", ".docx": "docx", ".md": "texto",
                  ".markdown": "texto", ".txt": "texto"}

_POR_MIME = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/markdown": "texto",
    "text/plain": "texto",
    "text/x-markdown": "texto",
}


def formato_de(path: Path, mime: str | None = None) -> str:
    """Formato efectivo. La extensión manda sobre el mime.

    Motivo: los navegadores mandan `application/octet-stream` para .md casi
    siempre, y con el mime como fuente primaria un markdown perfectamente
    estructurado acabaría en el camino del PDF y fallaría al abrirlo.
    """
    formato = _POR_EXTENSION.get(path.suffix.lower())
    if formato is None:
        formato = _POR_MIME.get((mime or "").split(";")[0].strip().lower())
    if formato is None:
        raise ValueError(f"formato no soportado: {path.name} ({mime})")
    return formato


def parsear(path: Path, mime: str | None = None) -> Documento:
    """Punto de entrada único de la ingesta.

    Cada formato tiene primario y respaldo, y el respaldo salta por dos motivos
    distintos: una excepción (el motor no pudo) o `SinTextoExtraible` (el motor
    pudo abrirlo pero no había texto). El segundo es el que importa: un PDF
    escaneado no lanza ninguna excepción en PyMuPDF, simplemente devuelve nada, y
    sin esta comprobación el documento entraría vacío y el agente creería que ese
    protocolo no dice nada en vez de avisar de que falló.
    """
    path = Path(path)
    formato = formato_de(path, mime)

    if formato == "texto":
        return texto_plano(path)

    intentos = (
        [pdf_con_pymupdf, con_docling] if formato == "pdf" else [con_docling, docx_minimo]
    )

    fallos: list[str] = []
    for motor in intentos:
        try:
            return motor(path)
        except Exception as exc:  # noqa: BLE001 — se prueba el siguiente motor
            fallos.append(f"{motor.__name__}: {exc}")

    raise RuntimeError(f"ningún motor pudo leer {path.name} — {' | '.join(fallos)}")
