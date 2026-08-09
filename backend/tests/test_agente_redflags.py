"""Banderas rojas sobre habla telefónica real, no sobre texto de laboratorio.

Todas las frases de este fichero están escritas como las diría un paciente por
teléfono y como las transcribiría Whisper `small`: sin tildes a medias, con los
números dictados con letra, con muletillas en medio y con frases que empiezan,
se corrigen y siguen.

**Los falsos positivos se prueban con tanto detalle como los negativos**, y ocupan
más casos que ellos. Un detector que se dispara con todo es igual de inútil que
uno que no se dispara nunca: si el agente corta el guion cada dos por tres, el
escalamiento deja de significar nada y la llamada deja de servir. Los casos que
más importan de este fichero son los del apartado «se parecen a una alarma y no lo
son»: fiebre que ya pasó, sangre al quitarse el apósito, dolor que no es el que
preocupa.

No hay ni un mock. `detectar()` es una función pura sobre una cadena: mockear algo
aquí sería mockear expresiones regulares.
"""

from __future__ import annotations

import pytest

from app.agent.redflags import (
    UMBRAL_FIEBRE,
    detectar,
    normalizar,
    peor,
    resumen,
)


def codigos(texto: str) -> list[str]:
    return [b.codigo for b in detectar(texto)]


# ---------------------------------------------------------------------------
# Normalización — lo que Whisper entrega de verdad
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "crudo,esperado",
    [
        # Números dictados. Es la forma NORMAL de decir una temperatura en voz.
        ("Tengo treinta y nueve", "tengo 39"),
        ("treinta y ocho y medio", "38.5"),
        ("treinta y ocho con cinco", "38.5"),
        ("cuarenta", "40"),
        ("treinta y siete", "37"),
        # El símbolo de grados y la coma decimal española.
        ("39,2 °C", "39.2 grados c"),
        ("38.9º", "38.9 grados"),
        # Tildes: Whisper las pone «casi siempre», que es lo peor de los dos mundos.
        ("Me duele el estómago", "me duele el estomago"),
        # Muletillas en medio de la frase.
        ("me duele, eh, mucho el pecho", "me duele, mucho el pecho"),
        ("pues o sea que no sé", "que no se"),
    ],
)
def test_normalizar_habla_telefonica(crudo: str, esperado: str) -> None:
    assert normalizar(crudo) == esperado


# ---------------------------------------------------------------------------
# Fiebre
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase",
    [
        "Pues mire, me tomé la temperatura hace un rato y tengo treinta y nueve",
        "tengo 39,2 de fiebre",
        "esta manana el termometro marcaba treinta y ocho con nueve",
        "eh, tengo fiebre, como treinta y nueve grados",
        "Tengo 38.9 grados",
        "me subió la fiebre a cuarenta",
        "la temperatura me dio treinta y nueve y medio",
        # Sin la palabra «grados» y sin decir «fiebre» hasta el final.
        "me lo acabo de tomar y marca treinta y nueve, tengo fiebre seguro",
        # Corrigiéndose a mitad de frase, que es como habla la gente.
        "tengo treinta y ocho, no, perdón, treinta y nueve de fiebre",
    ],
)
def test_fiebre_alta_dispara(frase: str) -> None:
    banderas = detectar(frase)
    assert "fiebre_alta" in [b.codigo for b in banderas], frase
    assert peor(banderas).urgencia == "urgente"


def test_fiebre_alta_lleva_los_grados_en_el_motivo() -> None:
    """El motivo va a `calls.escalation_reason` y lo lee una persona de triaje.

    «fiebre de 39.2 grados» le sirve para priorizar; «FIEBRE_ALTA» no."""
    bandera = detectar("tengo treinta y nueve coma dos de fiebre")[0]
    assert bandera.valor == pytest.approx(39.2)
    assert "39.2" in bandera.motivo


@pytest.mark.parametrize(
    "frase",
    [
        "tengo 38 grados",
        "la temperatura me dio 37.2",
        "tenía treinta y siete y pico esta mañana",
    ],
)
def test_febricula_no_escala(frase: str) -> None:
    """Por debajo del umbral el agente sigue el guion y lo anota, no corta."""
    assert codigos(frase) == []


def test_el_umbral_es_estricto() -> None:
    """38,5 clavados NO escala; 38,6 sí.

    Es una raya fina y discutible —hay protocolos que escalan ya en 38,5— y por
    eso está probada: si Samuel cambia `UMBRAL_FIEBRE`, este test le dice
    exactamente qué comportamiento acaba de cambiar."""
    assert UMBRAL_FIEBRE == 38.5
    assert codigos("tengo 38.5 grados de fiebre") == []
    assert "fiebre_alta" in codigos("tengo 38.6 grados de fiebre")


@pytest.mark.parametrize(
    "frase",
    [
        "estoy ardiendo de fiebre, no sé cuánto pero mucha",
        "tengo mucha fiebre desde anoche",
        "he tenido fiebre muy alta",
    ],
)
def test_fiebre_sin_numero_pero_alta(frase: str) -> None:
    """Sin termómetro, pero diciendo que es alta: escala como prioritaria.

    Decisión abierta: ver `eval/guion_llamada.md`. «Tengo fiebre» a secas NO
    escala —el agente pide el número—, y eso es el test siguiente."""
    assert "fiebre_referida" in codigos(frase)


def test_fiebre_a_secas_no_escala() -> None:
    assert codigos("pues sí, he tenido algo de fiebre") == []


def test_una_sola_bandera_por_familia() -> None:
    """Con número medido, la fiebre referida sobra: es el mismo hallazgo."""
    banderas = detectar("tengo muchísima fiebre, treinta y nueve y medio")
    assert [b.codigo for b in banderas] == ["fiebre_alta"]


# ---------------------------------------------------------------------------
# Sangrado
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase,codigo",
    [
        ("la herida no para de sangrar", "sangrado_activo"),
        ("me sale mucha sangre por la herida", "sangrado_activo"),
        ("tengo la gasa empapada de sangre", "sangrado_activo"),
        ("está empapando el apósito", "sangrado_activo"),
        ("la herida está sangrando", "sangrado"),
        ("me sale sangre por donde me operaron", "sangrado"),
        ("estoy sangrando", "sangrado"),
    ],
)
def test_sangrado_dispara(frase: str, codigo: str) -> None:
    assert codigo in codigos(frase)
    assert peor(detectar(frase)).urgencia == "urgente"


@pytest.mark.parametrize(
    "frase",
    [
        "solo veo una manchita de sangre en la gasa",
        "me sale un poquito de sangre, casi nada",
        "tiene una mancha de sangre pequeña pero nada más",
        "me sangró un poco al quitarme el apósito",
    ],
)
def test_sangrado_atenuado_no_escala(frase: str) -> None:
    """Manchar el apósito al día 1-3 es lo que dice el protocolo que pasará.

    Es la regla del módulo con la que menos cómodo estoy y está anotada como
    decisión pendiente en `eval/guion_llamada.md`: un paciente que quita
    importancia a lo suyo diría exactamente esto."""
    assert codigos(frase) == []


def test_atenuado_pero_con_intensificador_si_escala() -> None:
    """«Un poco» deja de valer en cuanto aparece «no para»."""
    assert "sangrado_activo" in codigos(
        "al principio era un poquito pero ahora no para de sangrar"
    )


# ---------------------------------------------------------------------------
# Dehiscencia
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase",
    [
        "se me abrió la herida",
        "se me soltaron los puntos",
        "se me han salido dos puntos",
        "la herida está abierta",
        "creo que se me despegó la cicatriz",
        "se me ha abierto un poco la herida por abajo",
    ],
)
def test_dehiscencia_dispara(frase: str) -> None:
    assert "dehiscencia" in codigos(frase)


def test_dehiscencia_no_la_atenua_el_diminutivo() -> None:
    """A diferencia del sangrado, «un poco abierta» sigue siendo dehiscencia.

    Una herida entreabierta no tiene una versión inocua: o está cerrada o no lo
    está. El atenuador solo aplica al sangrado."""
    assert "dehiscencia" in codigos("se me ha abierto un poquito la herida")


# ---------------------------------------------------------------------------
# Dolor torácico y disnea
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase,codigo",
    [
        ("me duele el pecho", "dolor_toracico"),
        ("me duele mucho aquí en el pecho", "dolor_toracico"),
        ("siento como una opresión en el pecho", "dolor_toracico"),
        ("tengo un peso en el pecho desde ayer", "dolor_toracico"),
        ("me duele, eh, bastante el pecho", "dolor_toracico"),
        ("no puedo respirar bien", "disnea"),
        ("me falta el aire cuando camino", "disnea"),
        ("me cuesta mucho respirar", "disnea"),
        ("me ahogo al subir las escaleras", "disnea"),
        ("me quedo sin aire con nada que haga", "disnea"),
    ],
)
def test_urgencias_respiratorias_y_cardiacas(frase: str, codigo: str) -> None:
    assert codigo in codigos(frase)


def test_no_puedo_respirar_lleva_su_propia_negacion() -> None:
    """El caso que más fácil se rompe al tocar el barrido de negaciones.

    «no puedo respirar» empieza por «no» y es una AFIRMACIÓN de síntoma; «no me
    falta el aire» empieza por «no» y es una negación. Los dos en el mismo test
    para que quien cambie `_esta_negado` los rompa a la vez y se entere."""
    assert "disnea" in codigos("no puedo respirar")
    assert codigos("no me falta el aire") == []


# ---------------------------------------------------------------------------
# Infección
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase",
    [
        "de la herida me sale un líquido amarillo",
        "le sale pus",
        "la herida huele mal",
        "la tengo roja y caliente",
        "está muy hinchada y roja",
        "me sale como un líquido verdoso",
    ],
)
def test_infeccion_dispara(frase: str) -> None:
    banderas = detectar(frase)
    assert "signos_infeccion" in [b.codigo for b in banderas]
    assert peor(banderas).urgencia == "prioritaria"


def test_enrojecimiento_solo_no_escala() -> None:
    """La herida roja al tercer día es normal. Roja Y caliente, no."""
    assert codigos("la herida está un poco roja") == []
    assert codigos("la veo algo enrojecida alrededor") == []
    assert "signos_infeccion" in codigos("la veo enrojecida y caliente")


# ---------------------------------------------------------------------------
# Negaciones — el paciente que está BIEN
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase",
    [
        "no tengo fiebre",
        "no, fiebre no he tenido nada",
        "no he tenido fiebre, estoy bien",
        "qué va, ninguna fiebre",
        "la herida no sangra",
        "no me sale sangre",
        "no ha sangrado nada",
        "no se me ha abierto la herida",
        "no me duele el pecho",
        "no tengo dolor en el pecho",
        "no me falta el aire",
        "no huele mal",
        "no le sale nada de pus",
        "sin fiebre y sin sangrado",
        "tampoco me falta el aire",
    ],
)
def test_negaciones_no_disparan(frase: str) -> None:
    assert codigos(frase) == [], frase


def test_la_negacion_no_se_come_la_frase_siguiente() -> None:
    """«No tengo fiebre PERO me sale sangre» tiene que escalar por el sangrado.

    Es el fallo peligroso del alcance por oración: una sola oración, dos
    hallazgos, y el segundo apagado por la negación del primero. Un falso
    negativo sobre una bandera roja es el error que este sistema no se puede
    permitir."""
    assert "sangrado" in codigos("no tengo fiebre pero me sale sangre")
    assert "sangrado_activo" in codigos("no tengo fiebre y me sale mucha sangre")
    assert "dehiscencia" in codigos("fiebre no, pero se me abrió la herida")


def test_no_se_si_no_es_una_negacion() -> None:
    """«No sé si...» es duda, no negación. Es la muletilla más común al teléfono.

    Se distingue de «no se me ha abierto», donde «se» es un clítico y sí niega."""
    assert "sangrado" in codigos("no sé si la herida está sangrando")
    assert codigos("no se me ha abierto la herida") == []


# ---------------------------------------------------------------------------
# Se parecen a una alarma y NO lo son
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase",
    [
        # Fiebre que ya pasó. El agente lo anota y sigue; no corta la llamada.
        "tuve fiebre pero ya se me quitó",
        "el primer día tuve algo de fiebre, ahora nada",
        # Números que no son grados.
        "tengo 39 años",
        "tomo cefalexina de 500 mg cada 8 horas",
        "me dijeron que tomara 40 gotas",
        "la cita es el día 20 a las 10 horas",
        "peso 38 kilos menos que antes de la operación",
        # Dolor que no es el que preocupa.
        "estoy bien, me duele un poco la herida al moverme",
        "me duele la espalda de estar tumbado",
        "me duele al toser pero es en la barriga",
        # Preguntas del paciente, no síntomas.
        "¿la fiebre es normal después de esto?",
        "¿y si me sangra qué hago?",
        # Lo que dice alguien que está perfectamente.
        "todo bien, la herida seca y sin dolor",
    ],
)
def test_falsos_positivos_que_arruinarian_la_llamada(frase: str) -> None:
    assert codigos(frase) == [], frase


# ---------------------------------------------------------------------------
# Varias a la vez
# ---------------------------------------------------------------------------
def test_varias_banderas_ordenadas_por_urgencia() -> None:
    banderas = detectar("tengo treinta y nueve de fiebre y la herida huele fatal, huele mal")
    assert [b.urgencia for b in banderas] == ["urgente", "prioritaria"]
    assert peor(banderas).codigo == "fiebre_alta"


def test_el_resumen_lista_todas() -> None:
    """El equipo clínico necesita ver los dos hallazgos al abrir el caso, no solo
    el peor: «tengo 39 y además sangra» son dos cosas que mirar."""
    texto = resumen(detectar("tengo treinta y nueve de fiebre y no para de sangrar"))
    assert "39" in texto
    assert "sangrado" in texto


# ---------------------------------------------------------------------------
# Bordes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("frase", ["", "   ", "eh", "mmm", "..."])
def test_entradas_vacias_o_ruido(frase: str) -> None:
    """Whisper devuelve esto continuamente sobre silencio y ruido de línea."""
    assert detectar(frase) == []


def test_la_evidencia_permite_auditar() -> None:
    """Cada bandera guarda el trozo que la disparó.

    Sin esto, un escalamiento es una afirmación sin respaldo; con esto se puede
    releer la llamada y ver si el detector acertó o si Whisper oyó mal."""
    bandera = detectar("pues mire, me tomé la temperatura y tengo treinta y nueve")[0]
    assert "39" in bandera.evidencia
    assert bandera.consulta_protocolo  # con qué preguntarle al RAG


def test_la_pregunta_del_guion_no_se_detecta_a_si_misma() -> None:
    """El detector solo ve el turno del PACIENTE, nunca el del agente.

    Este test fija por qué: el propio guion contiene la palabra fiebre y un
    número, y pasárselo haría que el agente se escalara a sí mismo en la primera
    pregunta. Si algún día alguien conecta `detectar()` al texto del agente, esto
    es lo que se rompe."""
    pregunta = "¿Ha tenido fiebre por encima de treinta y ocho grados y medio?"
    assert detectar(pregunta) == []
