"""Pruebas de detección de banderas rojas con regionalismos y habla coloquial colombiana.

Valida que frases cotidianas de pacientes colombianos activen correctamente
las alarmas deterministas en `app/agent/redflags.py`.
"""

import pytest
from app.agent.redflags import detectar


@pytest.mark.parametrize(
    "frase,codigo_esperado",
    [
        ("Siento una fatiga en el pecho horrible hace 20 minutos", "disnea"),
        ("Tengo asfixia y no me entra el aire", "disnea"),
        ("Me siento asfixiada y me cuesta respirar", "disnea"),
        ("Estoy ardiendo en calentura sin termómetro", "fiebre_referida"),
        ("Estoy volando en fiebre y con mucha templorina", "fiebre_referida"),
        ("Tengo 39.2 de fiebre y no me baja", "fiebre_alta"),
        ("Se me brotó un chichón con pus en la herida", "signos_infeccion"),
        ("Está brotando sangre a chorros por la cicatriz", "sangrado_activo"),
        ("Se me salieron los puntos y la herida está deshecha", "dehiscencia"),
        ("Me duele el pecho feo y siento una opresión", "dolor_toracico"),
    ],
)
def test_regionalismos_colombianos_activan_bandera_roja(frase, codigo_esperado):
    alarmas = detectar(frase)
    assert len(alarmas) >= 1, f"No se detectó alarma para la frase: '{frase}'"
    codigos = [a.codigo for a in alarmas]
    assert codigo_esperado in codigos, f"Esperado {codigo_esperado}, obtenido {codigos} en frase: '{frase}'"
