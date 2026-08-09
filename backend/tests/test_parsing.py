"""El parser tiene que devolver jerarquía, no solo texto.

Todo lo que se comprueba aquí es lo que `chunking.trocear()` necesita para
construir rutas «H2 › H3». Si estos tests pasan, el agente puede citar la sección
de la que sacó una respuesta; si fallan, puede seguir respondiendo, pero sin
poder decir de dónde — que en contexto clínico es no poder auditar nada.
"""

from pathlib import Path

import pytest

from app.rag import parsing
from app.rag.chunking import trocear

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "corpus_prueba"
PDF = CORPUS / "protocolo_apendicectomia.pdf"
DOCX = CORPUS / "protocolo_apendicectomia.docx"
MD = CORPUS / "protocolo_apendicectomia.md"

hay_corpus = pytest.mark.skipif(
    not PDF.exists(),
    reason="corre antes: uv run python ../eval/corpus_prueba/generar.py",
)


# ---------------------------------------------------------------------------
# Enrutado por formato
# ---------------------------------------------------------------------------
def test_la_extension_manda_sobre_el_mime():
    """Los navegadores mandan application/octet-stream para .md casi siempre.

    Si el mime decidiera, un markdown perfectamente estructurado acabaría en el
    camino del PDF y fallaría al abrirlo.
    """
    assert parsing.formato_de(Path("p.md"), "application/octet-stream") == "texto"
    assert parsing.formato_de(Path("p.pdf"), "application/octet-stream") == "pdf"


def test_mime_como_respaldo_sin_extension():
    assert parsing.formato_de(Path("adjunto"), "application/pdf") == "pdf"
    assert parsing.formato_de(Path("adjunto"), "text/plain; charset=utf-8") == "texto"


def test_formato_no_soportado():
    with pytest.raises(ValueError):
        parsing.formato_de(Path("radiografia.dcm"), "application/dicom")


# ---------------------------------------------------------------------------
# Ligaduras: el fallo más caro que tuvo este parser
# ---------------------------------------------------------------------------
def test_normalizar_deshace_la_ligadura_fi():
    """«ﬁebre» (U+FB01) no es «fiebre», y era lo que salía de los tres PDF.

    Sin NFKC, `to_tsvector('spanish', ...)` jamás casa la consulta «fiebre» con
    el texto del protocolo: la mitad léxica de la búsqueda híbrida se queda ciega
    justo en la palabra que dispara la escalada clínica.
    """
    crudo = "Acuda a urgencias si presenta ﬁebre superior a 38.5 grados"
    assert "fiebre" not in crudo
    assert "fiebre" in parsing.normalizar(crudo)


@hay_corpus
def test_el_pdf_no_deja_ligaduras():
    doc = parsing.pdf_con_pymupdf(PDF)
    assert "fiebre" in doc.markdown
    assert "ﬁ" not in doc.markdown and "ﬂ" not in doc.markdown


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
@hay_corpus
def test_pdf_recupera_la_jerarquia_completa():
    doc = parsing.pdf_con_pymupdf(PDF)
    esperados = [
        (1, "Protocolo de alta"),
        (2, "Cuidado de la herida"),
        (3, "Cómo curar la herida en casa"),
        (2, "Medicación"),
        (2, "Signos de alarma"),
        (3, "Cuándo llamar al equipo"),
        (2, "Actividad"),
        (2, "Dieta"),
    ]
    for nivel, titulo in esperados:
        assert f"\n{'#' * nivel} {titulo}" in f"\n{doc.markdown}" or any(
            linea.startswith(f"{'#' * nivel} ") and titulo in linea
            for linea in doc.markdown.splitlines()
        ), f"falta el encabezado de nivel {nivel}: {titulo}"


@hay_corpus
def test_un_encabezado_de_dos_lineas_es_un_solo_encabezado():
    """El título largo se maqueta en dos renglones del mismo tamaño.

    Sin unirlos, «…Apendicectomía / laparoscópica» generaba dos secciones y la
    segunda se llamaba «laparoscópica»: una sección fantasma a la que el troceado
    le cuelga contenido y que el agente acabaría citando.
    """
    encabezados = [
        linea for linea in parsing.pdf_con_pymupdf(PDF).markdown.splitlines()
        if linea.startswith("#")
    ]
    assert not any(linea.strip("# ").strip() == "laparoscópica" for linea in encabezados)
    assert any("Apendicectomía laparoscópica" in linea for linea in encabezados)


@hay_corpus
def test_pdf_conserva_la_tabla_de_dosis():
    """Fármaco y dosis tienen que quedar en la misma fila.

    Una tabla aplanada convierte «Cefalexina | 500 mg | cada 8 horas» en tres
    fragmentos sueltos, y entonces el agente puede recuperar «500 mg» sin saber
    de qué medicamento habla.
    """
    md = parsing.pdf_con_pymupdf(PDF).markdown
    fila = next((r for r in md.splitlines() if "Cefalexina" in r), "")
    assert "500 mg" in fila and "cada 8 horas" in fila


@hay_corpus
def test_pdf_no_duplica_el_texto_de_la_tabla():
    """La tabla se emite una vez: como tabla, no además como texto suelto."""
    assert parsing.pdf_con_pymupdf(PDF).markdown.count("Cefalexina") == 1


@hay_corpus
def test_pdf_sabe_en_que_pagina_va_cada_cosa():
    """Es lo que permite la cita «protocolo.pdf › Signos de alarma › p. 2»."""
    doc = parsing.pdf_con_pymupdf(PDF)
    assert doc.paginas == 2
    assert len(doc.saltos) == 2
    assert parsing.pagina_de(doc, 0) == 1
    assert parsing.pagina_de(doc, len(doc.markdown) - 1) == 2


@hay_corpus
def test_los_tres_formatos_dan_la_misma_jerarquia():
    """PDF, DOCX y MD son el mismo documento: deben trocearse igual.

    Es la comprobación que protege de que un camino se degrade en silencio. Si el
    parser de PDF perdiera niveles, aquí se vería como rutas distintas, no como
    una caída de calidad difusa en el retrieval.
    """
    rutas = {}
    for archivo in (PDF, DOCX, MD):
        trozos = trocear(parsing.parsear(archivo).markdown)
        rutas[archivo.suffix] = {
            t.heading.split(" › ", 1)[-1] for t in trozos if t.heading and "›" in t.heading
        }
    assert rutas[".pdf"] == rutas[".md"] == rutas[".docx"], rutas
    assert "Signos de alarma" in rutas[".pdf"]


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
@hay_corpus
def test_docx_minimo_lee_niveles_y_tabla():
    """El respaldo de .docx no adivina: lee el estilo del párrafo (w:pStyle)."""
    md = parsing.docx_minimo(DOCX).markdown
    assert "## Cuidado de la herida" in md
    assert "### Cómo curar la herida en casa" in md
    fila = next((r for r in md.splitlines() if "Cefalexina" in r), "")
    assert "500 mg" in fila


# ---------------------------------------------------------------------------
# Texto plano
# ---------------------------------------------------------------------------
def test_markdown_entra_tal_cual(tmp_path):
    origen = tmp_path / "protocolo.md"
    origen.write_text("# Título\n\nCuerpo del protocolo.\n", encoding="utf-8")
    doc = parsing.parsear(origen)
    assert doc.motor == "texto"
    assert doc.markdown.startswith("# Título")
    assert doc.paginas is None and doc.saltos == []


def test_archivo_vacio_no_pasa_por_bueno(tmp_path):
    """Entrar vacío es peor que fallar: el agente creería que ese protocolo no
    dice nada, en vez de avisar de que no se pudo leer."""
    vacio = tmp_path / "vacio.txt"
    vacio.write_text("   \n", encoding="utf-8")
    with pytest.raises(parsing.SinTextoExtraible):
        parsing.parsear(vacio)


# ---------------------------------------------------------------------------
# Primario y respaldo
# ---------------------------------------------------------------------------
def test_el_respaldo_entra_cuando_el_primario_revienta(tmp_path, monkeypatch):
    falso = tmp_path / "roto.pdf"
    falso.write_bytes(b"%PDF-1.4 basura")

    def _revienta(path):
        raise RuntimeError("PyMuPDF no pudo abrirlo")

    monkeypatch.setattr(parsing, "pdf_con_pymupdf", _revienta)
    monkeypatch.setattr(
        parsing, "con_docling",
        lambda path: parsing.Documento("# Rescatado\n\ntexto", "docling", 1),
    )
    assert parsing.parsear(falso).motor == "docling"


def test_el_respaldo_entra_tambien_si_el_primario_no_saca_texto(tmp_path, monkeypatch):
    """Un PDF escaneado no lanza excepción en PyMuPDF: devuelve nada.

    Es el caso que de verdad justifica mantener Docling instalado, y el que un
    respaldo que solo mirara excepciones dejaría pasar como documento vacío.
    """
    escaneado = tmp_path / "escaneado.pdf"
    escaneado.write_bytes(b"%PDF-1.4")

    def _sin_texto(path):
        raise parsing.SinTextoExtraible("el PDF no tiene capa de texto")

    monkeypatch.setattr(parsing, "pdf_con_pymupdf", _sin_texto)
    monkeypatch.setattr(
        parsing, "con_docling",
        lambda path: parsing.Documento("# Del OCR\n\ntexto reconocido", "docling", 1),
    )
    assert parsing.parsear(escaneado).motor == "docling"


def test_si_fallan_todos_los_motores_se_dice_por_que(tmp_path, monkeypatch):
    roto = tmp_path / "roto.pdf"
    roto.write_bytes(b"no soy un pdf")

    monkeypatch.setattr(parsing, "pdf_con_pymupdf", lambda p: (_ for _ in ()).throw(
        RuntimeError("cabecera inválida")))
    monkeypatch.setattr(parsing, "con_docling", lambda p: (_ for _ in ()).throw(
        RuntimeError("docling tampoco")))

    with pytest.raises(RuntimeError, match="cabecera inválida"):
        parsing.parsear(roto)
