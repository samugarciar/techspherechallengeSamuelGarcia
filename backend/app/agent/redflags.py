"""Detección determinista de signos de alarma sobre lo que dice el paciente.

**No pasa por el LLM, y eso es la decisión, no un detalle de implementación.**
Un modelo generativo con `temperature>0` puede clasificar la misma frase de dos
formas distintas en dos llamadas seguidas; si de esa clasificación depende que un
paciente que sangra reciba una instrucción de urgencia, la variabilidad no es
aceptable a ningún precio. Aquí un `re.search` da siempre el mismo veredicto, se
puede probar con 60 frases en 20 ms y se audita leyendo el patrón que disparó.
El LLM se usa para *hablar*; para *decidir si hay alarma*, no.

La entrada no es texto limpio: es la salida de Whisper `small` por teléfono, con
las tildes que le salgan, los números dictados con letra («treinta y nueve»),
muletillas en medio («me duele, eh, mucho el pecho») y frases sin terminar. Todo
el trabajo de este módulo está en `normalizar()` y en los huecos flexibles de los
patrones; sin eso, un detector que funciona sobre texto de laboratorio falla
sobre la primera transcripción real.

── Lo que NO dispara, y por qué ─────────────────────────────────────────────
Un detector que se dispara con todo es igual de inútil que uno que no se dispara
nunca: si en la demo los tres pacientes acaban escalados, el escalamiento deja de
significar nada. Dos atenuaciones deliberadas, y las dos están abiertas a que
Samuel las cambie (ver `eval/guion_llamada.md`):

- **Sangrado con diminutivo** («una manchita en la gasa», «unas gotitas») no es
  bandera roja. Al día 1-3 de una laparoscopia manchar el apósito es lo esperado.
  Basta un intensificador («mucha», «no para», «empapada») para que sí lo sea.
- **Enrojecimiento solo** no es bandera roja. La herida roja al tercer día es
  normal; roja **y** caliente, o roja e hinchada, ya no.

── Lo que sí es un compromiso peligroso ─────────────────────────────────────
`no puedo respirar` lleva su propia negación dentro. El barrido de negaciones
solo mira negadores que empiecen **antes** del comienzo de la coincidencia, así
que «no puedo respirar» dispara y «no me falta el aire» no. Es sutil y está
probado en `tests/test_agente_redflags.py`; cualquier patrón nuevo que empiece
por «no» tiene que llevar ese «no» dentro del propio patrón.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Urgencia = Literal["rutina", "prioritaria", "urgente"]

# El orden importa: es el de `calls.escalation_urgency` y el que usa `peor()`.
_PESO: dict[Urgencia, int] = {"rutina": 0, "prioritaria": 1, "urgente": 2}


@dataclass(slots=True)
class BanderaRoja:
    codigo: str
    motivo: str
    """Texto en español que va tal cual a `calls.escalation_reason` y a la
    pantalla. Se escribe pensando en que lo lea una enfermera de triaje, no un
    programador: «fiebre de 39.2 grados», no «FEVER_THRESHOLD_EXCEEDED»."""

    urgencia: Urgencia
    evidencia: str
    """El trozo exacto de la transcripción que disparó. Sin esto, un escalamiento
    es una afirmación sin respaldo; con esto se puede releer la llamada y
    comprobar si el detector acertó o si Whisper oyó mal."""

    consulta_protocolo: str
    """Con qué preguntarle al RAG por la instrucción de urgencia. El detector
    sabe QUÉ pasa; qué hay que decirle al paciente lo dice el protocolo del
    hospital, nunca este fichero. Ver `app/agent/tools.py:buscar_protocolo`."""

    valor: float | None = None
    """Solo para la fiebre: los grados detectados. Va al survey de la llamada."""


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------
_UNIDADES = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
}
_DECENAS = {"veinte": 20, "treinta": 30, "cuarenta": 40}

_ALT_UNI = "|".join(sorted(_UNIDADES, key=len, reverse=True))

# «treinta y nueve», «treinta y ocho y medio», «cuarenta», «treinta y ocho con
# cinco». Solo decenas 20/30/40 porque el rango útil es el de un termómetro: un
# conversor general de números en español sería más código y más formas de
# equivocarse para cubrir casos que un paciente no dice.
_NUMERO_EN_LETRA = re.compile(
    rf"\b(?P<dec>{'|'.join(_DECENAS)})"
    rf"(?:\s+y\s+(?P<uni>{_ALT_UNI}))?"
    rf"(?:\s+(?:y\s+(?P<medio>medio)|(?:con|coma|punto)\s+(?P<dec2>{_ALT_UNI}|\d)))?",
)

# Muletillas que Whisper transcribe y que parten los patrones por la mitad.
# Lista corta a propósito: quitar «bueno» o «este» sale barato hasta el día en
# que alguien dice «lo tengo bueno» o «este dolor», así que no entran.
_MULETILLAS = re.compile(r"\b(?:e+h+|a+h+|em+|mm+|umm+|o\s+sea|osea|digamos|pues)\b")


def _sin_tildes(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _a_digitos(m: re.Match[str]) -> str:
    entero = _DECENAS[m.group("dec")] + _UNIDADES.get(m.group("uni") or "", 0)
    if m.group("medio"):
        return f"{entero}.5"
    if (dec2 := m.group("dec2")) is not None:
        decimal = int(dec2) if dec2.isdigit() else _UNIDADES[dec2]
        return f"{entero}.{decimal}"
    return str(entero)


def normalizar(texto: str) -> str:
    """Transcripción cruda -> texto sobre el que los patrones son estables.

    Cinco pasos, y los cinco existen por un fallo concreto sobre habla real:
    sin tildes (Whisper las pone «casi siempre», que es lo peor de los dos
    mundos), grados escritos con símbolo, números dictados con letra, decimales
    dichos como «con cinco» o «y medio», y muletillas en medio de la frase.
    """
    t = _sin_tildes(texto.lower())
    t = re.sub(r"[°º]", " grados ", t)
    t = _NUMERO_EN_LETRA.sub(_a_digitos, t)
    # «38 y medio», «38 con 5», «38 coma 5» — la variante que ya venía en cifras.
    t = re.sub(r"\b(\d{2})\s+y\s+medio\b", r"\1.5", t)
    t = re.sub(r"\b(\d{2})\s*(?:con|coma|punto)\s*(\d)\b", r"\1.\2", t)
    t = re.sub(r"\b(\d{2}),(\d)\b", r"\1.\2", t)
    t = _MULETILLAS.sub(" ", t)
    # Quitar «eh» de «me duele, eh, mucho» deja «, ,». No afecta a la detección
    # —los patrones toleran comas— pero sí a `evidencia`, que se enseña.
    t = re.sub(r"\s*,(?:\s*,)+", ",", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# Negación
# ---------------------------------------------------------------------------
_NEGADORES = re.compile(
    r"\b(?:no|ni|nada\s+de|ning[uú]n|ninguna|ninguno|sin|tampoco|jam[aá]s|nunca|que\s+va)\b"
)

_NO_EPISTEMICO = re.compile(r"\s+s[eé]\s+(?:si|que|qu[eé]|cuant|cuand|como|bien|exact|muy|la\b)")
"""«no sé si la herida sangra» no niega nada: es duda, no negación.

Se distingue de «no se me ha abierto» —donde «se» es un clítico y sí hay
negación— por lo que viene después: una conjunción interrogativa o un adverbio
de duda. Sin esta excepción, la muletilla más común del español por teléfono
apagaría media detección."""

_NEGADOR_FALSO = re.compile(r"sin\s+(?:parar|cesar|aire|descanso|control|freno)\b")
"""«Sin» que no niega nada: forma parte del síntoma.

«sangra sin parar» y «me quedo sin aire» son las dos alarmas más claras que
existen, y las dos llevan un negador dentro. Sin esta excepción, el barrido de
negaciones las apagaba — y en silencio, que es lo peor: el detector no fallaba,
simplemente no encontraba nada."""

_VENTANA_NEGACION = 4
"""Palabras que alcanza una negación.

Es NegEx clásico. La alternativa que se descartó fue delimitar por oración:
«no tengo fiebre y me sale mucha sangre» tiene una sola oración, así que el
alcance por oración apagaría el sangrado — un falso NEGATIVO sobre una bandera
roja, que es justo el error que este sistema no se puede permitir. Con ventana
de 4, «no» cubre «tengo fiebre y me» y el sangrado sobrevive por una palabra.
Ese margen es estrecho a propósito: en la duda, este detector prefiere escalar
de más.
"""

_CORTE_CLAUSULA = re.compile(r"[,;.!?¿¡]|\bpero\b|\baunque\b|\bsino\b")


def _esta_negado(texto: str, inicio: int, fin: int | None = None) -> bool:
    """¿Hay una negación que alcance a esta coincidencia?

    Dos casos, y el segundo es el que costó encontrar:

    - **Negador delante** («no me falta el aire»): el patrón empieza en «me», el
      «no» queda a la izquierda y niega.
    - **Negador dentro** («la herida no sangra»): el patrón arranca en «la
      herida», así que el «no» cae *dentro* de la coincidencia. Mirar solo a la
      izquierda daría bandera roja a un paciente que está diciendo que está bien.

    La excepción es un negador que empiece exactamente donde empieza la
    coincidencia: eso significa que el patrón lleva su propia negación dentro
    —«no puedo respirar», «no para de sangrar»— y entonces el paciente está
    afirmando el síntoma, no negándolo.
    """
    fin = inicio if fin is None else fin
    for neg in _NEGADORES.finditer(texto, 0, fin):
        if neg.start() == inicio:
            continue
        if _NO_EPISTEMICO.match(texto, neg.end()):
            continue
        if _NEGADOR_FALSO.match(texto, neg.start()):
            continue
        limite = inicio if neg.end() <= inicio else fin
        entre = texto[neg.end():limite]
        # Una coma o un «pero» cierran el alcance: «no, la herida sangra».
        if _CORTE_CLAUSULA.search(entre):
            continue
        if len(entre.split()) <= _VENTANA_NEGACION:
            return True
    return False


# ---------------------------------------------------------------------------
# Patrones
# ---------------------------------------------------------------------------
def _hueco(n: int = 3) -> str:
    """Hasta `n` palabras sueltas entre dos anclas del patrón.

    Es lo que hace que «me duele el pecho» siga valiendo cuando el paciente dice
    «me duele bastante aquí en el pecho». No perezoso por gusto: con `*` el
    patrón se traga media frase y empieza a cruzar sujetos distintos.
    """
    return rf"(?:\w+\s+){{0,{n}}}?"


_H = _hueco()

# Cada entrada: (código, urgencia, motivo, consulta al RAG, patrón).
# El patrón se compila sobre texto ya normalizado: sin tildes, en minúsculas y
# con los números en cifras.
_REGLAS: list[tuple[str, Urgencia, str, str, str]] = [
    # --- sangrado -----------------------------------------------------------
    (
        "sangrado_activo", "urgente",
        "sangrado activo en la herida",
        "sangrado activo de la herida quirurgica que hacer",
        r"(?:"
        r"no\s+para\s+de\s+sangrar|no\s+deja\s+de\s+sangrar|"
        r"hemorragia|"
        r"(?:sangra|sangrando|sangrado|sangre)\s+(?:\w+\s+){0,2}?"
        r"(?:mucho|much[ao]|bastante|abundante|a\s+chorros|sin\s+parar)|"
        r"(?:mucha|bastante|demasiada)\s+sangre|"
        rf"empapad[ao]s?\s+{_H}(?:de\s+)?(?:sangre|gasa|aposito|venda)|"
        rf"empapando\s+{_H}(?:gasa|aposito|venda|vendaje)|"
        r"chorro\s+de\s+sangre|sangre\s+a\s+chorros"
        r")",
    ),
    (
        # Sangrado sin intensificador. Se separa del anterior para poder
        # atenuarlo: ver `_ATENUADORES`.
        "sangrado", "urgente",
        "sangrado de la herida",
        "sangrado de la herida quirurgica que hacer",
        r"(?:"
        rf"me\s+{_H}(?:sale|salio|esta\s+saliendo|sali[oa])\s+{_H}sangre|"
        rf"(?:la\s+)?(?:herida|cicatriz|incision)\s+{_H}(?:sangra|esta\s+sangrando|sangro)|"
        r"esta\s+sangrando|estoy\s+sangrando|sangrando\s+por\s+(?:la\s+)?herida"
        r")",
    ),
    # --- dehiscencia --------------------------------------------------------
    (
        "dehiscencia", "urgente",
        "la herida se ha abierto (dehiscencia)",
        "dehiscencia herida abierta puntos sueltos que hacer",
        r"(?:"
        r"dehiscencia|"
        rf"se\s+{_H}(?:abrio|ha\s+abierto|abrieron|han\s+abierto|despego|solto|soltaron|"
        rf"salieron|salio|salido|reventaron|revento|rompio)\s+{_H}"
        r"(?:la\s+|el\s+|los\s+|las\s+)?(?:herida|cicatriz|incision|punto|puntos|grapa|grapas|sutura)|"
        rf"(?:herida|cicatriz|incision)\s+{_H}(?:esta\s+|se\s+ve\s+)?abiert[ao]|"
        r"se\s+me\s+(?:ve|ven)\s+(?:por\s+dentro|la\s+carne|las\s+tripas)"
        r")",
    ),
    # --- dolor torácico -----------------------------------------------------
    (
        "dolor_toracico", "urgente",
        "dolor torácico",
        "dolor toracico postoperatorio urgencia que hacer",
        r"(?:"
        r"dolor\s+toracico|"
        rf"(?:dolor|opresion|presion|peso|aprieta|ardor)\s+{_H}(?:en\s+)?(?:el\s+)?pecho|"
        rf"me\s+duele\s+{_H}(?:el\s+)?pecho|"
        r"(?:pecho|torax)\s+(?:me\s+)?(?:duele|aprieta)|"
        r"me\s+aprieta\s+el\s+pecho"
        r")",
    ),
    # --- disnea -------------------------------------------------------------
    (
        "disnea", "urgente",
        "dificultad respiratoria",
        "dificultad para respirar disnea postoperatoria urgencia",
        r"(?:"
        r"disnea|"
        r"no\s+puedo\s+respirar|no\s+consigo\s+respirar|"
        rf"me\s+(?:falta|faltaba|esta\s+faltando)\s+{_H}(?:el\s+)?aire|"
        rf"me\s+(?:cuesta|costaba)\s+{_H}respirar|"
        r"me\s+ahogo|me\s+estoy\s+ahogando|sensacion\s+de\s+ahogo|"
        r"dificultad\s+(?:para|al)\s+respirar|"
        r"me\s+quedo\s+sin\s+aire|"
        r"(?:respiro|respirar)\s+con\s+dificultad"
        r")",
    ),
    # --- infección ----------------------------------------------------------
    (
        "signos_infeccion", "prioritaria",
        "signos de infección en la herida",
        "signos de infeccion de la herida quirurgica pus enrojecimiento",
        r"(?:"
        r"\bpus\b|purulent[ao]|supura|supurando|"
        rf"(?:sale|saliendo|salir)\s+{_H}(?:un\s+|una\s+)?(?:liquido|material|secrecion)\s+"
        r"(?:\w+\s+){0,2}?(?:amarill[ao]|verd[eo]s?|verdos[ao]|con\s+mal\s+olor|maloliente)|"
        r"(?:liquido|material|secrecion)\s+(?:amarill[ao]|verdos[ao]|purulent[ao])|"
        rf"(?:huele\s+mal|mal\s+olor|olor\s+feo|olor\s+fetido)"
        r")",
    ),
    (
        # Enrojecimiento CON otro signo inflamatorio. Solo, no dispara: ver la
        # cabecera del módulo.
        "signos_infeccion", "prioritaria",
        "herida enrojecida con signos inflamatorios",
        "signos de infeccion de la herida quirurgica enrojecimiento calor",
        r"(?:"
        rf"(?:roj[ao]|enrojecid[ao]|colorad[ao])\s+{_H}"
        r"(?:y\s+)?(?:caliente|hinchad[ao]|inflamad[ao]|dura|endurecid[ao])|"
        rf"(?:caliente|hinchad[ao]|inflamad[ao])\s+{_H}(?:y\s+)?(?:roj[ao]|enrojecid[ao])|"
        r"(?:cada\s+vez\s+mas|mas)\s+roj[ao]\s+(?:e\s+|y\s+)?(?:hinchad[ao]|inflamad[ao]|caliente)"
        r")",
    ),
    # --- fiebre sin número --------------------------------------------------
    (
        "fiebre_referida", "prioritaria",
        "fiebre alta referida sin termómetro",
        "fiebre despues de la cirugia cuando consultar",
        r"(?:"
        r"(?:mucha|much[ií]sima|bastante|alt[ai]sima)\s+fiebre|"
        r"fiebre\s+(?:muy\s+)?(?:alta|altisima|fuerte)|"
        r"(?:ardiendo|hirviendo)\s+(?:de\s+fiebre|en\s+fiebre)|"
        r"estoy\s+ardiendo|"
        r"fiebrones?"
        r")",
    ),
]

# Los patrones se escriben con `\s+` y se compilan con `[\s,]+`. La coma de una
# pausa —«me duele, mucho, el pecho»— parte cualquier patrón de varias palabras,
# y Whisper puntúa las vacilaciones del habla telefónica con comas casi siempre.
# Se hace en la compilación y no a mano en cada patrón para que el patrón siga
# leyéndose como la frase que representa.
_COMPILADAS = [
    (codigo, urgencia, motivo, consulta, re.compile(patron.replace(r"\s+", r"[\s,]+")))
    for codigo, urgencia, motivo, consulta, patron in _REGLAS
]

# Una bandera por familia clínica. Sin esto, «la herida no para de sangrar»
# devuelve `sangrado_activo` y `sangrado`, y el equipo clínico abre el caso
# creyendo que hay dos hallazgos donde hay uno.
_FAMILIA = {
    "fiebre_alta": "fiebre",
    "fiebre_referida": "fiebre",
    "sangrado_activo": "sangrado",
    "sangrado": "sangrado",
}

# Atenuadores del sangrado. Si aparecen a la izquierda de la coincidencia y no
# hay intensificador, no se escala: manchar la gasa al tercer día es lo previsto
# por el protocolo, no una urgencia.
_ATENUADORES = re.compile(
    r"\b(?:un\s+poquit[oa]|un\s+poco|poquit[oa]|manchit[ao]|gotit?as?|unas?\s+gotas?|"
    r"muy\s+poc[oa]|casi\s+nada|apenas|leve|minim[ao]|puntit[oa]|una\s+mancha)\b"
)

# ---------------------------------------------------------------------------
# Fiebre con número
# ---------------------------------------------------------------------------
UMBRAL_FIEBRE = 38.5
"""Grados por encima de los cuales se escala. `>`, no `>=`: 38.5 exactos no
escalan.

Sale de `docs/CONTRATO_LLAMADAS.md` («fiebre >38,5°»). Es una raya fina y
discutible —muchos protocolos escalan ya en 38.5— y por eso está aquí como
constante con nombre y no incrustada en una comparación. Pendiente de que Samuel
la confirme contra el protocolo real del hospital."""

_PISTAS_FIEBRE = re.compile(
    r"\b(?:fiebre|febril|febricula|calentura|temperatura|termometro|grados|decimas|"
    r"destemplad[ao]|tirit(?:o|ando|ona)|escalofrios)\b"
)

# Unidades que descartan que ese número sean grados. Sin esto, «tomo 500 mg cada
# 8 horas» y «tengo 39 años» acabarían de bandera roja.
_UNIDAD_NO_TEMPERATURA = re.compile(
    r"^\s*(?:anos|ano|kilos|kilo|kg|mg|miligramos|gramos|g|ml|"
    r"horas|hora|dias|dia|minutos|semanas|veces|puntos|pastillas|comprimidos|"
    r"por\s+ciento|%)\b"
)

_NUMERO = re.compile(r"\b(\d{2}(?:\.\d)?)\b")
_VENTANA_PISTA = 45
"""Caracteres a cada lado del número donde se busca una pista de temperatura."""


def _fiebre(texto: str) -> BanderaRoja | None:
    """Temperatura por encima del umbral, dicha como sea.

    Se exige una pista de temperatura cerca del número, y no basta con que el
    número esté en el rango de un termómetro: «me tomo 40 gotas» está en rango y
    no es fiebre. Al revés, con la pista, «tengo 39» sin la palabra «grados» sí
    lo es, que es como habla la gente por teléfono.
    """
    for m in _NUMERO.finditer(texto):
        valor = float(m.group(1))
        if not (35.0 <= valor <= 43.0):
            continue
        if _UNIDAD_NO_TEMPERATURA.match(texto[m.end():]):
            continue
        ventana = texto[max(0, m.start() - _VENTANA_PISTA): m.end() + _VENTANA_PISTA]
        if not _PISTAS_FIEBRE.search(ventana):
            continue
        if valor < UMBRAL_FIEBRE:
            continue
        if _esta_negado(texto, m.start()):
            continue
        return BanderaRoja(
            codigo="fiebre_alta",
            motivo=f"fiebre de {m.group(1)} grados",
            urgencia="urgente",
            evidencia=_recorte(texto, m.start(), m.end()),
            consulta_protocolo="fiebre alta despues de la cirugia que hacer urgencia",
            valor=valor,
        )
    return None


def _recorte(texto: str, inicio: int, fin: int, margen: int = 30) -> str:
    return texto[max(0, inicio - margen): min(len(texto), fin + margen)].strip()


# ---------------------------------------------------------------------------
# API del módulo
# ---------------------------------------------------------------------------
def detectar(texto: str) -> list[BanderaRoja]:
    """Banderas rojas en lo que ha dicho **el paciente**, de mayor a menor urgencia.

    OJO con a qué se le pasa el texto: solo al turno del paciente, nunca al del
    agente. El propio guion contiene la frase «¿ha tenido fiebre por encima de 38
    grados?», y pasársela a este detector haría que el agente se escalara a sí
    mismo en la primera pregunta. Quien orquesta la llamada
    (`app/agent/agente.py`) lo respeta; si aparece otro llamante, que lo respete
    también.
    """
    if not texto or not texto.strip():
        return []

    t = normalizar(texto)
    encontradas: list[BanderaRoja] = []
    vistos: set[str] = set()

    if (fiebre := _fiebre(t)) is not None:
        encontradas.append(fiebre)
        vistos.add("fiebre")

    for codigo, urgencia, motivo, consulta, patron in _COMPILADAS:
        familia = _FAMILIA.get(codigo, codigo)
        if familia in vistos:
            continue
        m = patron.search(t)
        if m is None or _esta_negado(t, m.start(), m.end()):
            continue
        if codigo == "sangrado" and _ATENUADORES.search(t):
            # Atenuado y sin intensificador (la regla `sangrado_activo` va antes
            # y ya habría entrado si lo hubiera). No se escala.
            continue
        vistos.add(familia)
        encontradas.append(
            BanderaRoja(
                codigo=codigo,
                motivo=motivo,
                urgencia=urgencia,
                evidencia=_recorte(t, m.start(), m.end()),
                consulta_protocolo=consulta,
            )
        )

    encontradas.sort(key=lambda b: _PESO[b.urgencia], reverse=True)
    return encontradas


def peor(banderas: list[BanderaRoja]) -> BanderaRoja | None:
    """La que manda cuando hay varias. `detectar` ya devuelve ordenado."""
    return banderas[0] if banderas else None


def resumen(banderas: list[BanderaRoja]) -> str:
    """Motivo compuesto para `calls.escalation_reason`.

    Se listan todas y no solo la peor: si el paciente dice «tengo 39 y la herida
    sangra», el equipo clínico necesita ver las dos cosas al abrir el caso.
    """
    return "; ".join(b.motivo for b in banderas)
