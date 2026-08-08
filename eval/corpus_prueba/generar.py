"""Deriva los .pdf y .docx del corpus provisional a partir de los .md.

Los tres formatos tienen que decir EXACTAMENTE lo mismo. Solo así una diferencia
entre lo que recupera el RAG desde el PDF y lo que recupera desde el markdown se
puede atribuir al parser y no al contenido — que es justo lo que mide
`scripts/spikes/spike_parsing.py`.

Los PDF se generan sin marcadores (outline) a propósito: un protocolo escaneado o
exportado desde Word rara vez los trae, y `app/rag/parsing.py` no debe depender
de ellos. Los encabezados se distinguen por tamaño de fuente, que es lo único con
lo que se puede contar siempre.

    cd backend && uv run python ../eval/corpus_prueba/generar.py
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

AQUI = Path(__file__).resolve().parent

# Cuerpo a 10 pt y saltos claros entre niveles. Los saltos importan: el parser de
# PDF agrupa los tamaños de fuente y necesita que un H3 no se confunda con el
# cuerpo por un punto de diferencia.
CSS = """
body  { font-family: sans-serif; font-size: 10px; line-height: 1.4; }
h1    { font-size: 20px; margin-bottom: 8px; }
h2    { font-size: 15px; margin-top: 12px; margin-bottom: 6px; }
h3    { font-size: 12px; margin-top: 10px; margin-bottom: 4px; }
p     { margin-bottom: 6px; }
table { border: 1px solid #444; margin-bottom: 8px; }
th    { border: 1px solid #444; padding: 3px; }
td    { border: 1px solid #444; padding: 3px; }
"""


@dataclass(slots=True)
class Bloque:
    tipo: str                                  # 'h' | 'p' | 'tabla' | 'ol'
    nivel: int = 0                             # solo 'h'
    texto: str = ""                            # solo 'h' | 'p'
    filas: list[list[str]] = field(default_factory=list)   # solo 'tabla'
    items: list[str] = field(default_factory=list)         # solo 'ol'


_H = re.compile(r"^(#{1,6})\s+(.*)$")
_FILA = re.compile(r"^\s*\|(.+)\|\s*$")
_SEPARADOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_ITEM = re.compile(r"^\s*\d+\.\s+(.*)$")


def leer_bloques(markdown: str) -> list[Bloque]:
    """Subconjunto de markdown suficiente para estos protocolos.

    No es un parser de markdown de propósito general y no pretende serlo: solo
    tiene que entender lo que hay en `eval/corpus_prueba/*.md`.
    """
    bloques: list[Bloque] = []
    parrafo: list[str] = []

    def cerrar_parrafo() -> None:
        if parrafo:
            bloques.append(Bloque("p", texto=" ".join(parrafo)))
            parrafo.clear()

    lineas = markdown.splitlines()
    i = 0
    while i < len(lineas):
        linea = lineas[i]

        if not linea.strip():
            cerrar_parrafo()
            i += 1
            continue

        if m := _H.match(linea):
            cerrar_parrafo()
            bloques.append(Bloque("h", nivel=len(m.group(1)), texto=m.group(2).strip()))
            i += 1
            continue

        if _FILA.match(linea):
            cerrar_parrafo()
            filas: list[list[str]] = []
            while i < len(lineas) and _FILA.match(lineas[i]):
                if not _SEPARADOR.match(lineas[i]):
                    celdas = [c.strip() for c in lineas[i].strip().strip("|").split("|")]
                    filas.append(celdas)
                i += 1
            bloques.append(Bloque("tabla", filas=filas))
            continue

        if _ITEM.match(linea):
            cerrar_parrafo()
            items: list[str] = []
            while i < len(lineas) and (lineas[i].strip() or items):
                if m := _ITEM.match(lineas[i]):
                    items.append(m.group(1).strip())
                elif lineas[i].startswith(("   ", "\t")) and items:
                    items[-1] += " " + lineas[i].strip()   # continuación indentada
                else:
                    break
                i += 1
            bloques.append(Bloque("ol", items=items))
            continue

        parrafo.append(linea.strip())
        i += 1

    cerrar_parrafo()
    return bloques


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _a_html(bloques: list[Bloque]) -> str:
    partes: list[str] = []
    for b in bloques:
        match b.tipo:
            case "h":
                partes.append(f"<h{b.nivel}>{_esc(b.texto)}</h{b.nivel}>")
            case "p":
                partes.append(f"<p>{_esc(b.texto)}</p>")
            case "ol":
                items = "".join(f"<li>{_esc(x)}</li>" for x in b.items)
                partes.append(f"<ol>{items}</ol>")
            case "tabla":
                filas = []
                for n, fila in enumerate(b.filas):
                    etiqueta = "th" if n == 0 else "td"
                    celdas = "".join(f"<{etiqueta}>{_esc(c)}</{etiqueta}>" for c in fila)
                    filas.append(f"<tr>{celdas}</tr>")
                partes.append(f"<table>{''.join(filas)}</table>")
    return f"<html><body>{''.join(partes)}</body></html>"


def escribir_pdf(bloques: list[Bloque], destino: Path) -> int:
    story = pymupdf.Story(html=_a_html(bloques), user_css=CSS)
    marco = pymupdf.Rect(50, 55, 545, 790)

    escritor = pymupdf.DocumentWriter(str(destino))
    quedan = 1
    while quedan:
        dispositivo = escritor.begin_page(pymupdf.paper_rect("a4"))
        quedan, _ = story.place(marco)
        story.draw(dispositivo)
        escritor.end_page()
    escritor.close()

    with pymupdf.open(destino) as doc:
        return doc.page_count


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
# Se escribe el OOXML a mano en vez de añadir python-docx: la dependencia solo
# haría falta aquí, para generar material de prueba que se tira en cuanto llegue
# el corpus real. Docling lee el .docx por el nombre del estilo del párrafo, así
# que basta con declarar Heading1..3 en styles.xml y referenciarlos con w:pStyle.
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Los URI de OOXML no caben en 100 columnas y no se pueden partir en el XML, así
# que se parten en el literal de Python: la concatenación reconstruye el mismo URI.
_CABECERA = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_TIPOS = "application/vnd.openxmlformats-officedocument.wordprocessingml"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CONTENT_TYPES = (
    _CABECERA
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package'
    + '.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/>'
    + f'<Override PartName="/word/document.xml" ContentType="{_TIPOS}.document.main+xml"/>'
    + f'<Override PartName="/word/styles.xml" ContentType="{_TIPOS}.styles+xml"/>'
    + "</Types>"
)


def _relaciones(tipo: str, destino: str) -> str:
    return (
        _CABECERA
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006'
        + '/relationships">'
        + f'<Relationship Id="rId1" Type="{_REL}/{tipo}" Target="{destino}"/>'
        + "</Relationships>"
    )


_RELS = _relaciones("officeDocument", "word/document.xml")
_DOC_RELS = _relaciones("styles", "styles.xml")


def _styles_xml() -> str:
    estilos = [
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>'
    ]
    for n, tam in ((1, 40), (2, 30), (3, 26)):   # media-puntos: 20 pt, 15 pt, 13 pt
        estilos.append(
            f'<w:style w:type="paragraph" w:styleId="Heading{n}">'
            f'<w:name w:val="heading {n}"/>'
            f'<w:basedOn w:val="Normal"/>'
            f'<w:pPr><w:outlineLvl w:val="{n - 1}"/></w:pPr>'
            f'<w:rPr><w:b/><w:sz w:val="{tam}"/></w:rPr></w:style>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{_NS_W}">{"".join(estilos)}</w:styles>'
    )


def _parrafo(texto: str, estilo: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{estilo}"/></w:pPr>' if estilo else ""
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{_esc(texto)}</w:t></w:r></w:p>'


def _tabla(filas: list[list[str]]) -> str:
    ancho = max((len(f) for f in filas), default=0)
    borde = (
        "<w:tblBorders>"
        + "".join(
            f'<w:{lado} w:val="single" w:sz="4" w:space="0" w:color="444444"/>'
            for lado in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders>"
    )
    columna = 9000 // max(ancho, 1)
    # w:tblGrid es obligatorio en OOXML y Word siempre lo emite. Sin él, los
    # lectores que se apoyan en python-docx (Docling entre ellos) no ven ninguna
    # columna y descartan la tabla entera en silencio — se comprobó: la tabla de
    # dosis desaparecía del markdown sin un solo error.
    rejilla = "".join(f'<w:gridCol w:w="{columna}"/>' for _ in range(ancho))
    xml = [f"<w:tbl><w:tblPr>{borde}</w:tblPr><w:tblGrid>{rejilla}</w:tblGrid>"]
    for fila in filas:
        celdas = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="{columna}" w:type="dxa"/></w:tcPr>'
            f"{_parrafo(c)}</w:tc>"
            for c in fila + [""] * (ancho - len(fila))
        )
        xml.append(f"<w:tr>{celdas}</w:tr>")
    xml.append("</w:tbl>")
    # Word exige un párrafo tras una tabla; sin él, algunos lectores la ignoran.
    xml.append("<w:p/>")
    return "".join(xml)


def escribir_docx(bloques: list[Bloque], destino: Path) -> None:
    cuerpo: list[str] = []
    for b in bloques:
        match b.tipo:
            case "h":
                cuerpo.append(_parrafo(b.texto, f"Heading{min(b.nivel, 3)}"))
            case "p":
                cuerpo.append(_parrafo(b.texto))
            case "ol":
                # Numeración literal en el texto en lugar de <w:numPr>: la lista
                # de numeración de OOXML exige numbering.xml entero y aquí solo
                # importa que el orden llegue intacto al markdown.
                cuerpo.extend(_parrafo(f"{n}. {x}") for n, x in enumerate(b.items, 1))
            case "tabla":
                cuerpo.append(_tabla(b.filas))

    documento = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_NS_W}"><w:body>{"".join(cuerpo)}</w:body></w:document>'
    )

    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _styles_xml())
        z.writestr("word/document.xml", documento)


def main() -> None:
    fuentes = sorted(AQUI.glob("protocolo_*.md"))
    if not fuentes:
        raise SystemExit(f"no hay ningún protocolo_*.md en {AQUI}")

    for md in fuentes:
        bloques = leer_bloques(md.read_text(encoding="utf-8"))
        paginas = escribir_pdf(bloques, md.with_suffix(".pdf"))
        escribir_docx(bloques, md.with_suffix(".docx"))
        encabezados = sum(1 for b in bloques if b.tipo == "h")
        print(
            f"{md.stem:32s} {len(bloques):3d} bloques  {encabezados:2d} encabezados  "
            f"{paginas} pág.  -> .pdf .docx"
        )


if __name__ == "__main__":
    main()
