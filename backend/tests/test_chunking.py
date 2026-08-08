from app.rag.chunking import (
    MAX_CHARS,
    MIN_CHARS,
    Trozo,
    texto_a_embeber,
    trocear,
)

PROTOCOLO = """# Protocolo de alta — Apendicectomía laparoscópica

Documento de referencia para el seguimiento telefónico del paciente.

## Cuidado de la herida

Mantenga los apósitos limpios y secos. Puede ducharse a partir de las 48 horas
de la intervención, secando la zona con una toalla limpia sin frotar. No aplique
cremas ni pomadas sobre las incisiones salvo indicación médica expresa.

## Signos de alarma

Acuda al servicio de urgencias si presenta fiebre superior a 38.5 grados,
sangrado activo por las incisiones, dehiscencia de la herida, dolor abdominal
intenso que no cede con la analgesia pautada, o vómitos persistentes.

### Cuándo llamar al equipo

Si el dolor aumenta de forma progresiva durante más de 24 horas.

## Medicación

Cefalexina 500 mg cada 8 horas durante 7 días. Acetaminofén 1 g cada 8 horas
si hay dolor. No suspenda el antibiótico aunque se sienta mejor.
"""


def test_trocea_por_secciones():
    trozos = trocear(PROTOCOLO)
    assert len(trozos) >= 3

    headings = [t.heading for t in trozos if t.heading]
    assert any("Cuidado de la herida" in h for h in headings)
    assert any("Signos de alarma" in h for h in headings)
    assert any("Medicación" in h for h in headings)


def test_jerarquia_en_el_heading():
    """Los encabezados llevan la ruta completa, para que la cita sea navegable."""
    rutas = [t.heading for t in trocear(PROTOCOLO) if t.heading]
    assert any("›" in r for r in rutas), f"sin jerarquía en {rutas}"


def test_seccion_corta_se_funde_con_su_padre_no_con_la_siguiente():
    """Una subsección corta NO puede acabar etiquetada como la sección siguiente.

    `### Cuándo llamar al equipo` cuelga de `## Signos de alarma`. Si se fundiera
    hacia adelante quedaría bajo `## Medicación`, archivando una regla de alarma
    bajo el epígrafe equivocado — y el agente la citaría mal por voz.
    """
    trozos = trocear(PROTOCOLO)
    dueño = next(t for t in trozos if "el dolor aumenta de forma progresiva" in t.content)

    assert "Signos de alarma" in (dueño.heading or "")
    assert "Medicación" not in (dueño.heading or "")
    # El subencabezado se pierde como etiqueta pero sobrevive en el texto.
    assert "Cuándo llamar al equipo" in dueño.content


def test_ordinales_consecutivos_desde_cero():
    """La UNIQUE (document_id, ordinal) del schema depende de esto."""
    trozos = trocear(PROTOCOLO)
    assert [t.ordinal for t in trozos] == list(range(len(trozos)))


def test_regla_clinica_no_se_parte():
    """La regla de fiebre debe caber entera en un trozo.

    Es el fallo que hace inútil un RAG clínico: si "fiebre superior a" y
    "38.5 grados" caen en trozos distintos, ninguna de las dos mitades de la
    búsqueda recupera la regla completa.
    """
    trozos = trocear(PROTOCOLO)
    assert any("fiebre superior a 38.5 grados" in t.content for t in trozos)


def test_secciones_cortas_se_funden():
    """Un encabezado con una línea suelta no debe generar su propio trozo."""
    for t in trocear(PROTOCOLO):
        assert t.n_chars >= MIN_CHARS or t is trocear(PROTOCOLO)[-1]


def test_respeta_max_chars_con_solape():
    largo = "# Sección larga\n\n" + ("Esta es una frase de relleno del protocolo. " * 120)
    trozos = trocear(largo)
    assert len(trozos) > 1
    for t in trozos:
        # +OVERLAP margen: el solape puede empujar ligeramente el último trozo
        assert t.n_chars <= MAX_CHARS + 200


def test_documento_sin_encabezados():
    trozos = trocear("Texto plano sin ninguna estructura de encabezados. " * 10)
    assert len(trozos) == 1
    assert trozos[0].heading is None


def test_texto_a_embeber_antepone_heading():
    t = Trozo(ordinal=0, content="Mantenerla seca 48 horas.", heading="Cuidado de la herida")
    assert texto_a_embeber(t).startswith("Cuidado de la herida")
    assert "Mantenerla seca" in texto_a_embeber(t)

    sin = Trozo(ordinal=0, content="Sin encabezado.", heading=None)
    assert texto_a_embeber(sin) == "Sin encabezado."


def test_documento_vacio():
    assert trocear("") == []
