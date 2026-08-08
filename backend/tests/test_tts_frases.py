"""El troceado por frases es la optimización de latencia más importante del
pipeline de voz: permite empezar a hablar tras la primera frase (461 ms) en vez
de esperar al párrafo completo (987 ms).

Si parte mal, el agente dice cosas como «tuvo fiebre de treinta y ocho punto» /
«cinco grados» con una pausa en medio. Por eso se prueba aparte.
"""

from app.voice.tts import dividir_en_frases


def test_divide_por_frases():
    frases = dividir_en_frases(
        "Buenos días, le llamo del hospital. ¿Cómo ha evolucionado el dolor? "
        "Necesito hacerle unas preguntas rápidas."
    )
    assert len(frases) == 3
    assert frases[1].startswith("¿Cómo")


def test_no_parte_decimales():
    """«fiebre superior a 38.5 grados» debe salir de una sola pieza."""
    frases = dividir_en_frases("Acuda a urgencias si tiene fiebre superior a 38.5 grados.")
    assert len(frases) == 1
    assert "38.5 grados" in frases[0]


def test_no_parte_abreviaturas():
    """«Dr. Peláez» partido suena a «...el doctor» / pausa / «Peláez...»."""
    frases = dividir_en_frases("Su cirujano fue el Dr. Peláez y todo salió bien.")
    assert len(frases) == 1


def test_no_parte_en_tratamiento_ni_direccion():
    assert dividir_en_frases("La Dra. Carmona le atenderá en la revisión.") == [
        "La Dra. Carmona le atenderá en la revisión."
    ]
    assert len(dividir_en_frases("Su cita es en la Av. Libertador, torre B.")) == 1


def test_simbolos_de_unidad_si_cierran_frase():
    """«mg», «ml», «kg» se escriben sin punto en español, así que un punto
    detrás sí cierra frase. Tratarlos como abreviatura impediría un corte
    legítimo y retrasaría el primer audio."""
    frases = dividir_en_frases("Tome 500 mg. Después descanse ocho horas.")
    assert len(frases) == 2
    assert frases[0] == "Tome 500 mg."
    assert frases[1].startswith("Después")


def test_no_parte_iniciales():
    frases = dividir_en_frases("El paciente J. Villalba evoluciona favorablemente.")
    assert len(frases) == 1


def test_muletillas_se_pegan_pero_no_arrastran_a_la_frase_larga():
    """«Sí.» no merece su propia llamada al motor, pero fundir TODO retrasa el
    primer audio. Las muletillas se agrupan entre sí; la frase con contenido
    sale aparte para poder empezar a hablar antes."""
    frases = dividir_en_frases("Sí. Entiendo. Vamos a revisar cómo va su herida ahora.")
    assert len(frases) == 2
    assert frases[0] == "Sí. Entiendo."
    assert frases[1].startswith("Vamos a revisar")


def test_respeta_signos_de_apertura():
    frases = dividir_en_frases("Perfecto, lo anoto. ¿Ha tenido algún sangrado?")
    assert len(frases) == 2
    assert frases[1].startswith("¿")


def test_texto_vacio():
    assert dividir_en_frases("") == []
    assert dividir_en_frases("   ") == []


def test_frase_unica_sin_puntuacion_final():
    assert dividir_en_frases("Le llamo para su seguimiento") == [
        "Le llamo para su seguimiento"
    ]
