"""El guion de la llamada: bloque común + preguntas propias de la cirugía.

Decisión 1 de `docs/CONTRATO_LLAMADAS.md`. Preguntarle a un colecistectomizado
por la tolerancia a las grasas y a un herniorrafiado por los esfuerzos no es
decoración clínica: es la diferencia entre un cuestionario genérico y un
seguimiento que un cirujano reconocería como suyo.

── Por qué el guion es DATOS y no una instrucción del prompt ────────────────
La alternativa era escribir «pregúntale por el dolor, la herida, la fiebre…» en
el system prompt y dejar que el modelo improvisara el orden y la redacción. Se
descartó por tres motivos, y el tercero es el que decide:

1. Una pregunta clínica redactada de dos formas distintas recoge dos respuestas
   distintas. Si el texto lo genera el modelo en cada llamada, dos pacientes no
   contestan a lo mismo y el `survey` deja de ser comparable.
2. Con el guion en datos, `registrar_respuesta` puede **rechazar claves que no
   existen**. Sin él, el modelo inventa `dolor_nivel`, `dolor_escala` y
   `nivel_dolor` en tres llamadas y el histórico no se puede consultar.
3. Es lo que Samuel tiene que poder revisar y corregir sin leer Python. La
   versión en prosa de este fichero está en `eval/guion_llamada.md`, y ese es el
   documento que valida un clínico. Este módulo es su reflejo ejecutable: si los
   dos divergen, manda el de prosa.

El modelo sigue eligiendo **cuándo** preguntar cada cosa, cómo encadenar con lo
que el paciente acaba de decir y cuándo repreguntar. Lo que no elige es qué se
pregunta ni con qué nombre se guarda.
"""

from __future__ import annotations

from dataclasses import dataclass

Bloque = str


@dataclass(slots=True, frozen=True)
class Pregunta:
    clave: str
    """Nombre bajo el que la respuesta se guarda en `calls.survey`. Es la clave
    que `registrar_respuesta` valida, así que cambiarla rompe el histórico."""

    texto: str
    """Redacción de referencia, en español hablado. El agente puede adaptarla al
    hilo de la conversación —y debe— pero no cambiar lo que se pregunta."""

    bloque: Bloque


# ---------------------------------------------------------------------------
# Bloque común — lo que se pregunta en todo seguimiento postoperatorio
# ---------------------------------------------------------------------------
# El orden no es arbitrario: se empieza por el dolor porque es lo que el paciente
# tiene en la cabeza y contesta sin esfuerzo, y se dejan la medicación y la
# movilidad para después, cuando ya está cómodo hablando. Las dudas van al final
# porque abren conversación y no conviene abrirla en medio del cuestionario.
_ANTES = [
    Pregunta("dolor", "¿Cómo va el dolor? Del uno al diez, ¿dónde lo pondría hoy?", "comun"),
    Pregunta(
        "dolor_control",
        "¿Con las pastillas que le mandaron se le calma, o se le queda igual?",
        "comun",
    ),
    Pregunta(
        "herida",
        "Cuénteme cómo tiene la herida. ¿La ve limpia y seca, o le sale algo?",
        "comun",
    ),
    Pregunta(
        "fiebre",
        "¿Ha tenido fiebre? Si se ha tomado la temperatura, dígame cuánto le marcó.",
        "comun",
    ),
]

_DESPUES = [
    Pregunta(
        "medicacion",
        "¿Está tomando la medicación como se la mandaron, sin saltarse tomas?",
        "comun",
    ),
    Pregunta(
        "movilidad",
        "¿Se está levantando y caminando un poco por la casa?",
        "comun",
    ),
    Pregunta(
        "dudas",
        "¿Hay algo que le preocupe o alguna duda que quiera que le resuelva?",
        "cierre",
    ),
]

MARCA_ESPECIFICO = "especifico"
"""Las preguntas propias de la cirugía se insertan entre `_ANTES` y `_DESPUES`.

Van ahí y no al final porque son las que un paciente percibe como «esta llamada
sabe de lo mío»: colocarlas después de las dudas, cuando la conversación ya se
está cerrando, desperdicia ese efecto."""


# ---------------------------------------------------------------------------
# Bloques por procedimiento
# ---------------------------------------------------------------------------
# La clave es `surgeries.protocol_tag`, que es la MISMA con la que se acota la
# búsqueda en el RAG. Un solo identificador para las dos cosas: si mañana entra
# una gastrectomía, se añade su etiqueta aquí y su protocolo al corpus, y nada
# más hay que tocar.
POR_PROTOCOLO: dict[str, list[Pregunta]] = {
    "apendicectomia": [
        Pregunta(
            "transito",
            "¿Ha podido ir al baño? ¿Ha expulsado gases con normalidad?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "tolerancia_dieta",
            "¿Está tolerando bien la comida, o le dan náuseas o vómitos?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "dolor_hombro",
            "¿Ha notado dolor en el hombro? Es frecuente después de una laparoscopia.",
            MARCA_ESPECIFICO,
        ),
    ],
    "colecistectomia": [
        Pregunta(
            "tolerancia_grasas",
            "¿Cómo le están sentando las comidas con grasa? ¿Le dan diarrea o le caen mal?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "color_piel_orina",
            "¿Ha notado la piel o los ojos amarillos, o la orina más oscura de lo normal?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "dolor_hombro",
            "¿Le ha dolido el hombro derecho? Después de esta cirugía es habitual.",
            MARCA_ESPECIFICO,
        ),
    ],
    "herniorrafia": [
        Pregunta(
            "esfuerzos",
            "¿Ha cargado algo de peso o ha hecho algún esfuerzo estos días?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "hinchazon_ingle",
            "¿Ha notado la ingle hinchada o algún moretón grande por la zona?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "orinar",
            "¿Está orinando con normalidad, sin molestias ni dificultad?",
            MARCA_ESPECIFICO,
        ),
    ],
    "colectomia": [
        Pregunta(
            "transito_intestinal",
            "¿Ha estado evacuando el intestino y expulsando gases con normalidad?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "tolerancia_alimentos",
            "¿Está tolerando la dieta suave o le dan cólicos fuertes después de comer?",
            MARCA_ESPECIFICO,
        ),
    ],
    "reemplazo_articular": [
        Pregunta(
            "movilidad_articulación",
            "¿Ha estado realizando los ejercicios de movilidad recomendados para su prótesis?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "hinchazon_pierna",
            "¿Tiene la pierna o el tobillo muy hinchados o dolor en la pantorrilla?",
            MARCA_ESPECIFICO,
        ),
    ],
    "mastectomia": [
        Pregunta(
            "drenaje_herida",
            "¿Cómo está funcionando el drenaje? ¿Ha notado secreción abundante o mal olor?",
            MARCA_ESPECIFICO,
        ),
        Pregunta(
            "movilidad_brazo",
            "¿Ha podido mover suavemente el brazo del lado operado según las indicaciones?",
            MARCA_ESPECIFICO,
        ),
    ],
}

# Si `protocol_tag` viene vacío —un alta cargada a mano, un procedimiento que
# nadie etiquetó— se deduce del nombre. No es adivinar: es el mismo mapeo que
# haría una persona leyendo «Colecistectomía laparoscópica». Sin esta red, un
# `protocol_tag` a NULL degrada la llamada al cuestionario genérico en silencio.
_ALIAS: dict[str, str] = {
    "apendic": "apendicectomia",
    "apendice": "apendicectomia",
    "colecist": "colecistectomia",
    "vesicula": "colecistectomia",
    "hernio": "herniorrafia",
    "hernia": "herniorrafia",
    "eventro": "herniorrafia",
    "colectom": "colectomia",
    "colon": "colectomia",
    "cadera": "reemplazo_articular",
    "rodilla": "reemplazo_articular",
    "articular": "reemplazo_articular",
    "mastectom": "mastectomia",
    "seno": "mastectomia",
    "mama": "mastectomia",
}


def etiqueta_de(protocol_tag: str | None, procedure_name: str | None = None) -> str | None:
    """Etiqueta de protocolo aplicable, deducida del nombre si hace falta."""
    if protocol_tag and protocol_tag in POR_PROTOCOLO:
        return protocol_tag
    nombre = (procedure_name or "").lower()
    for fragmento, etiqueta in _ALIAS.items():
        if fragmento in nombre:
            return etiqueta
    return protocol_tag or None


def guion_para(protocol_tag: str | None, procedure_name: str | None = None) -> list[Pregunta]:
    """Guion completo y ordenado para esta cirugía.

    Con una etiqueta desconocida devuelve solo el bloque común, que sigue siendo
    un seguimiento válido. Se prefirió eso a fallar: una cirugía sin protocolo
    cargado no debe dejar al paciente sin llamada.
    """
    etiqueta = etiqueta_de(protocol_tag, procedure_name)
    especificas = POR_PROTOCOLO.get(etiqueta or "", [])
    return [*_ANTES, *especificas, *_DESPUES]


CLAVES_LIBRES = frozenset({"otros_sintomas"})
"""Claves que no son preguntas del guion pero que el agente puede registrar.

Existe una sola: lo que el paciente cuenta por su cuenta y no encaja en ninguna
pregunta («me duele al toser»). Sin ella, el modelo intentaría meterlo en la
clave más parecida y contaminaría un campo estructurado con texto suelto."""


def claves_validas(protocol_tag: str | None, procedure_name: str | None = None) -> set[str]:
    """Claves que `registrar_respuesta` acepta para esta llamada."""
    return {p.clave for p in guion_para(protocol_tag, procedure_name)} | set(CLAVES_LIBRES)


def como_texto(preguntas: list[Pregunta]) -> str:
    """El guion tal y como entra en el system prompt.

    Numerado y con la clave visible: el número le da al modelo el orden por
    defecto y la clave le dice con qué nombre registrar la respuesta, sin
    necesidad de una segunda lista que se pueda desincronizar de ésta.
    """
    return "\n".join(
        f"{n}. [{p.clave}] {p.texto}" for n, p in enumerate(preguntas, start=1)
    )
