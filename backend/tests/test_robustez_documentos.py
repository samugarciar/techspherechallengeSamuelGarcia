"""Documentos venenosos: lo que llega cuando el administrador no es sintético.

El camino feliz ya está probado. Lo que hunde una demo clínica es el otro: un PDF
escaneado, uno cifrado, uno de 0 bytes, un .docx que en realidad es otra cosa. La
garantía que se defiende aquí tiene dos mitades y la segunda es la que importa:

  1. Ninguno tumba el worker ni deja el documento colgado en un estado intermedio.
  2. **Ninguno llega a 'ready' sin haberse aprendido de verdad.** Fallar es
     aceptable; fallar en silencio no. Un documento en 'ready' con un pie de
     página por todo contenido pinta en la consola «Listo — el agente ya lo sabe»
     y el administrador se entera de que no sabe nada cuando el paciente pregunta.

El caso rector es el PDF escaneado, porque es el que de verdad va a aparecer: un
escáner de hospital produce páginas de imagen y estampa encima un pie de página
de texto («Hospital General · Página 1 de 2»). Ese pie basta para que la
extracción directa «funcione» y para que el respaldo con OCR no llegue a
ejecutarse nunca.
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import numpy as np
import pymupdf
import pytest

from app.core.config import REPO_ROOT, Settings, get_settings
from app.db.pool import connection
from app.rag import embeddings, ingest, parsing
from app.rag.chunking import trocear
from tests.ayuda_db import (  # noqa: F401 — `pool` es una fixture
    contar_recuperables,
    crear_documento,
    hay_postgres,
    limpiar_documentos,
    pool,
)
from tests.test_parsing import PDF

hay_corpus = pytest.mark.skipif(
    not PDF.exists(), reason="corre antes: uv run python ../eval/corpus_prueba/generar.py"
)


# ---------------------------------------------------------------------------
# Fabricación de venenos
# ---------------------------------------------------------------------------
def _rasterizar(origen: Path, destino: Path, pie: str | None = None, dpi: int = 100) -> Path:
    """Convierte un PDF en su versión escaneada: páginas de imagen.

    `pie` estampa una línea de texto por página, que es lo que hace un escáner de
    hospital de verdad. Es justo esa línea la que convierte un fallo ruidoso
    («no hay capa de texto») en uno silencioso («he aprendido 66 caracteres»).
    """
    with pymupdf.open(origen) as src, pymupdf.open() as dst:
        for n, pagina in enumerate(src, start=1):
            pix = pagina.get_pixmap(dpi=dpi)
            nueva = dst.new_page(width=pagina.rect.width, height=pagina.rect.height)
            nueva.insert_image(nueva.rect, pixmap=pix)
            if pie:
                nueva.insert_text((40, nueva.rect.height - 20), pie.format(n=n), fontsize=8)
        dst.save(destino, deflate=True)
    return destino


def _cifrado(origen: Path, destino: Path) -> Path:
    with pymupdf.open(origen) as doc:
        doc.save(destino, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    return destino


def venenos(carpeta: Path) -> dict[str, Path]:
    """Los venenos que no dependen del corpus. Nombre -> ruta ya escrita."""
    hechos = {
        # Cabecera de PDF creíble y nada detrás.
        "corrupto.pdf": b"%PDF-1.4\n" + b"\x00\xff" * 500,
        "cero.pdf": b"",
        "cero.docx": b"",
        "cero.md": b"",
        # Un PNG con la extensión cambiada: el caso «arrastré el archivo que no era».
        "mentiroso.pdf": b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
        # Un zip que se corta a la primera cabecera.
        "roto.docx": b"PK\x03\x04 pero aqui se acaba el zip",
        # Markdown que es todo esqueleto y nada de carne.
        "solo_encabezados.md": b"# Protocolo\n\n## Herida\n\n### Curas\n\n#### Notas\n",
    }
    salida = {}
    for nombre, datos in hechos.items():
        ruta = carpeta / nombre
        ruta.write_bytes(datos)
        salida[nombre] = ruta
    return salida


# ---------------------------------------------------------------------------
# El caso rector: PDF escaneado
# ---------------------------------------------------------------------------
@hay_corpus
def test_un_escaneado_con_sello_no_cuela_como_documento_legible(tmp_path):
    """66 caracteres de pie de página no son un protocolo aprendido.

    Sin este umbral la extracción directa «acierta», el respaldo con OCR no se
    dispara nunca, y el documento entra en la base con un único fragmento que
    dice «Hospital General · Página 1 de 2». La consola muestra 'ready' y el
    administrador cree que el agente conoce el protocolo.
    """
    sello = _rasterizar(PDF, tmp_path / "escaneado_sellado.pdf",
                        pie="Hospital General · Pagina {n} de 2")

    with pytest.raises(parsing.SinTextoExtraible):
        parsing.pdf_con_pymupdf(sello)


@hay_corpus
def test_un_escaneado_sin_nada_de_texto_tampoco(tmp_path):
    """El caso evidente, por si el umbral tapara el detector original."""
    puro = _rasterizar(PDF, tmp_path / "escaneado_puro.pdf")
    with pytest.raises(parsing.SinTextoExtraible):
        parsing.pdf_con_pymupdf(puro)


@hay_corpus
def test_al_escaneado_lo_rescata_el_ocr_de_respaldo(tmp_path):
    """Que falle no basta: un protocolo escaneado hay que aprenderlo igual.

    Es el único motivo por el que Docling sigue instalado. Cuesta ~20 s frente a
    los 0.15 s de la extracción directa, y por eso el umbral tiene que estar
    afinado para no mandar por aquí documentos que sí tienen capa de texto.
    """
    sello = _rasterizar(PDF, tmp_path / "escaneado_ocr.pdf",
                        pie="Hospital General · Pagina {n} de 2")

    t0 = time.perf_counter()
    doc = parsing.parsear(sello)
    print(f"\nOCR de respaldo: {time.perf_counter() - t0:.1f} s, "
          f"{len(doc.markdown)} caracteres")

    assert doc.motor == "docling", "el respaldo con OCR no llegó a entrar"
    assert len(doc.markdown) > 1000, "el OCR devolvió menos texto que un pie de página"
    assert "fiebre" in doc.markdown.lower()
    assert len(trocear(doc.markdown)) > 1


@hay_corpus
def test_si_ni_el_ocr_saca_texto_se_dice_que_hay_que_escanearlo(tmp_path, monkeypatch):
    """El mensaje que ve el administrador tiene que ser accionable.

    Se simula que el OCR tampoco encuentra nada (sin modelos, sin red, o un
    escaneo ilegible). Lo que no puede pasar es que la consola muestre
    «pdf_con_pymupdf: ...» y el administrador no sepa qué hacer con el archivo.
    """
    sello = _rasterizar(PDF, tmp_path / "sin_rescate.pdf", pie="Pagina {n}")

    def _ocr_a_ciegas(path):
        raise parsing.SinTextoExtraible("docling no extrajo texto")

    monkeypatch.setattr(parsing, "con_docling", _ocr_a_ciegas)

    with pytest.raises(parsing.SinTextoExtraible) as fallo:
        parsing.parsear(sello)

    mensaje = str(fallo.value).lower()
    assert "escaneado" in mensaje or "capa de texto" in mensaje
    assert "ocr" in mensaje
    assert mensaje.startswith("no se pudo"), f"el mensaje no empieza en español: {mensaje}"


@hay_corpus
def test_un_pdf_protegido_lo_dice_sin_gastar_el_ocr(tmp_path, monkeypatch):
    """Una contraseña no la arregla ningún respaldo: se avisa y se para.

    Pasar por Docling cuesta ~3 s para acabar diciendo «docling-parse could not
    load document 4be239a4», que no le dice nada a nadie.
    """
    cifrado = _cifrado(PDF, tmp_path / "cifrado.pdf")

    def _no_deberia_llamarse(path):
        raise AssertionError("un PDF con contraseña no debe pasar por el OCR")

    monkeypatch.setattr(parsing, "con_docling", _no_deberia_llamarse)

    with pytest.raises(parsing.PdfProtegido) as fallo:
        parsing.parsear(cifrado)
    assert "contraseña" in str(fallo.value).lower()


# ---------------------------------------------------------------------------
# El resto del muestrario
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nombre",
    ["corrupto.pdf", "cero.pdf", "cero.docx", "cero.md", "mentiroso.pdf", "roto.docx"],
)
def test_lo_ilegible_se_explica_en_castellano(tmp_path, nombre):
    """El texto del error acaba en la consola tal cual. Tiene que ser legible.

    Antes salía «MsWordDocumentBackend could not load document with hash
    e3b0c442…», que es correcto y completamente inútil para quien tiene que
    decidir si vuelve a exportar el archivo o busca otro.
    """
    ruta = venenos(tmp_path)[nombre]

    with pytest.raises(Exception) as fallo:  # noqa: B017 — el tipo da igual, el texto no
        parsing.parsear(ruta)

    mensaje = str(fallo.value)
    assert mensaje.startswith("No se pudo") or "vacío" in mensaje, mensaje
    assert nombre in mensaje, "el mensaje no dice de qué archivo habla"


def test_un_markdown_de_solo_encabezados_no_deja_nada_que_aprender(tmp_path):
    """El esqueleto de un protocolo no es el protocolo.

    Se trocea a cero fragmentos, y la ingesta lo convierte en 'failed'. Lo que no
    puede es llegar a 'ready' con chunks_count 0: sería el mismo fallo silencioso
    que el escaneado, por otra puerta.
    """
    ruta = venenos(tmp_path)["solo_encabezados.md"]
    assert trocear(parsing.parsear(ruta).markdown) == []


def test_el_texto_plano_tambien_se_normaliza(tmp_path):
    """Un .md con ligaduras o espacios de ancho cero es tan ilegible como un PDF.

    El camino de texto plano se saltaba `normalizar()` porque «ya es la
    representación de destino». No lo es: un .md exportado desde Word o pegado
    desde un PDF trae «ﬁebre» (U+FB01) y espacios de ancho cero, y entonces
    `to_tsvector('spanish', …)` no casa jamás la palabra que dispara la escalada
    clínica. El fallo es invisible: el documento entra en 'ready' con su texto.
    """
    ruta = tmp_path / "sucio.md"
    ruta.write_text(
        "# Signos de alarma\n\nAcuda a urgencias si presenta ﬁe​bre "
        "superior a 38.5 grados.\x00\x07\n",
        encoding="utf-8",
    )

    md = parsing.parsear(ruta).markdown
    assert "fiebre" in md, "la ligadura o el espacio de ancho cero sobrevivieron"
    assert "\x00" not in md and "\x07" not in md
    assert "38.5 grados" in md


# ---------------------------------------------------------------------------
# Contra la base de datos: nada llega a 'ready' sin haberse aprendido
# ---------------------------------------------------------------------------
async def _ingerir(ruta: Path, mime: str = "application/pdf"):
    """Copia el archivo a storage, crea la fila y lo procesa como el worker."""
    destino = get_settings().storage_dir / f"{uuid4().hex}_{ruta.name}"
    destino.write_bytes(ruta.read_bytes())
    document_id = await crear_documento(ruta.name, destino, uuid4().hex, mime=mime)
    resultado = await ingest.procesar_documento(document_id)
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT status, error_message, chunks_count FROM documents WHERE id = %s",
            (document_id,),
        )
        doc = await cur.fetchone()
    return document_id, destino, resultado, doc


@hay_postgres
@hay_corpus
async def test_un_escaneado_no_llega_a_ready_con_cero_fragmentos(pool, tmp_path,  # noqa: F811
                                                                 monkeypatch):
    """El fallo que la consola no distinguiría de un éxito.

    Se corta el OCR a propósito para probar el peor caso: sin respaldo, el
    documento tiene que quedarse en 'failed' con un motivo, nunca en 'ready'.
    """
    monkeypatch.setattr(
        parsing, "con_docling",
        lambda path: (_ for _ in ()).throw(parsing.SinTextoExtraible("sin OCR")),
    )
    sello = _rasterizar(PDF, tmp_path / "escaneado.pdf", pie="Hospital · Pagina {n}")

    document_id, destino, resultado, doc = await _ingerir(sello)
    try:
        assert not resultado.ok
        assert doc["status"] == "failed", (
            f"un escaneado llegó a '{doc['status']}' con {doc['chunks_count']} fragmentos"
        )
        assert "escaneado" in doc["error_message"].lower() \
            or "capa de texto" in doc["error_message"].lower()
        assert await contar_recuperables(document_id) == 0
    finally:
        await limpiar_documentos(document_id)
        destino.unlink(missing_ok=True)


def test_la_carpeta_de_almacenamiento_no_depende_del_directorio_de_trabajo():
    """`STORAGE_DIR=./storage/documents` es relativo, y eso rompe dos cosas.

    Se resuelve contra el directorio de trabajo del proceso, así que la API
    arrancada desde `backend/` y un script lanzado desde la raíz del repo usan
    carpetas DISTINTAS. Dos consecuencias, la segunda grave:

      - El worker no encuentra el archivo que escribió la API y todo documento
        acaba en 'failed' por «no se pudo leer», sin que nada apunte al motivo.
      - `olvidar_documento` solo borra el archivo físico si está por debajo de
        `storage_dir` —una comprobación correcta, contra travesías de ruta—. Con la
        ruta relativa esa comparación falla cuando el proceso que borra tiene otro
        cwd que el que escribió, y el `except OSError: pass` se lo traga: la fila
        desaparece, el agente olvida, y el PDF con datos clínicos se queda en el
        disco del hospital para siempre. Un olvido a medias que nadie ve.
    """
    ajustes = Settings(storage_dir=Path("./storage/documents"))
    assert ajustes.storage_dir.is_absolute(), (
        "storage_dir sigue siendo relativa: depende de dónde se arranque el proceso"
    )
    assert ajustes.storage_dir == (REPO_ROOT / "storage" / "documents").resolve()


@hay_postgres
async def test_olvidar_borra_el_archivo_aunque_cambie_el_directorio_de_trabajo(
    pool, tmp_path, monkeypatch  # noqa: F811
):
    """La mitad del olvido que vive fuera de Postgres.

    El CASCADE se lleva los vectores en la transacción, pero el archivo original
    se borra después y con una comprobación de ruta. Si esa comprobación depende
    del cwd, el borrado deja el documento en disco sin decir nada.
    """
    ruta, _ = tmp_path, None  # tmp_path solo para tener un cwd al que saltar
    destino = get_settings().storage_dir / f"{uuid4().hex}_para_borrar.md"
    destino.write_text("# Alta\n\nContenido clínico del paciente.", encoding="utf-8")
    document_id = await crear_documento("para_borrar.md", destino, uuid4().hex,
                                        mime="text/markdown")
    try:
        monkeypatch.chdir(ruta)      # otro proceso, otro directorio de trabajo
        assert await ingest.olvidar_documento(document_id) is True
        assert not destino.exists(), (
            "el documento se olvidó en la base pero el archivo sigue en el disco"
        )
    finally:
        await limpiar_documentos(document_id)
        destino.unlink(missing_ok=True)


@hay_postgres
async def test_ningun_veneno_deja_el_documento_colgado(pool, tmp_path):  # noqa: F811
    """Todos acaban en 'failed' con un motivo. Ninguno en un estado intermedio.

    Un documento parado en 'chunking' para siempre es peor que uno en 'failed':
    la consola lo pinta en ámbar («Troceando»), el administrador espera, y no
    hay nada que esperar.
    """
    mimes = {".pdf": "application/pdf", ".md": "text/markdown",
             ".docx": "application/vnd.openxmlformats-officedocument."
                      "wordprocessingml.document"}
    creados = []
    try:
        for nombre, ruta in venenos(tmp_path).items():
            document_id, destino, resultado, doc = await _ingerir(
                ruta, mime=mimes[Path(nombre).suffix]
            )
            creados.append((document_id, destino))

            assert not resultado.ok, f"{nombre} se dio por bueno"
            assert doc["status"] == "failed", f"{nombre} quedó en '{doc['status']}'"
            assert doc["error_message"], f"{nombre} falló sin decir por qué"
            assert await contar_recuperables(document_id) == 0
    finally:
        for document_id, destino in creados:
            await limpiar_documentos(document_id)
            destino.unlink(missing_ok=True)


@hay_postgres
async def test_los_caracteres_de_control_no_impiden_aprender(pool, tmp_path,  # noqa: F811
                                                             monkeypatch):
    """Un NUL no puede costar el documento entero.

    Postgres rechaza el byte 0x00 dentro de una columna `text`, así que sin
    saneado el INSERT de la promoción revienta y un protocolo perfectamente
    legible acaba en 'failed' por un carácter invisible. Y con el saneado tiene
    que quedar además BUSCABLE: es lo que comprueba la consulta léxica del final.
    """
    async def _vectores(textos, batch_size=16):
        return np.random.default_rng(0).standard_normal((len(textos), 1024)).astype(np.float32)

    monkeypatch.setattr(embeddings, "embeber_lote", _vectores)

    sucio = tmp_path / "sucio.md"
    sucio.write_text(
        "# Signos de alarma\n\nAcuda a urgencias si presenta ﬁe​bre superior a "
        "38.5 grados\x00, o si la herida supura.\x07 Mantenga el apósito seco durante "
        "las primeras 48 horas y no se duche hasta pasado ese plazo.\n",
        encoding="utf-8",
    )

    document_id, destino, resultado, doc = await _ingerir(sucio, mime="text/markdown")
    try:
        assert resultado.ok, f"el NUL tumbó la ingesta: {resultado.error}"
        assert doc["status"] == "ready"

        async with connection() as conn:
            cur = await conn.execute(
                """
                SELECT count(*) AS n FROM retrievable_chunks
                 WHERE document_id = %s
                   AND content_tsv @@ websearch_to_tsquery('spanish', 'fiebre')
                """,
                (document_id,),
            )
            assert (await cur.fetchone())["n"] > 0, (
                "el índice léxico no encuentra «fiebre»: la ligadura o el espacio "
                "de ancho cero llegaron hasta la base de datos"
            )
    finally:
        await limpiar_documentos(document_id)
        destino.unlink(missing_ok=True)
