"""El prompt de sistema del agente clínico, y las frases que NO genera el modelo.

Dos ideas gobiernan este fichero.

**1. Lo que la ley exige no se le pide a un modelo, se escribe.** El AI Act
obliga a que una persona sepa que está hablando con una IA. Si esa frase la
redacta el modelo en cada llamada, un día saldrá suavizada, otro día se la comerá
al encadenar con un «buenos días» del paciente, y no hay forma de demostrar que
se dijo. `SALUDO` es una constante: se pronuncia siempre igual, se puede citar en
una auditoría, y de paso ahorra el primer viaje al LLM —el saludo sale por el TTS
mientras el modelo aún no ha recibido nada—. Lo mismo vale para `FRASE_SEGURIDAD`.

**2. El prompt manda sobre el estilo y sobre los límites; no sobre los hechos.**
Aquí no hay ni un dato clínico ni un dato del paciente más allá de su nombre de
pila y su procedimiento, que hacen falta para saludar y para elegir el guion.
Todo lo demás lo traen las herramientas en el momento de necesitarlo. Un prompt
que llevara la medicación dentro estaría repitiendo el error que
`app/agent/tools.py` explica: una foto tomada al empezar la llamada.

── Longitud ────────────────────────────────────────────────────────────────
Este prompt cuesta latencia en cada turno, y en voz la latencia se oye. Está
escrito corto a propósito: son las reglas que cambian el comportamiento y ni una
frase más de relleno cortés. El TTFT medido con este prompt exacto está en el
README §Presupuesto de latencia.
"""

from __future__ import annotations

from app.agent.guion import como_texto, guion_para
from app.agent.tools import ContextoLlamada

SALUDO = (
    "Buenos días. Le llamo del servicio de seguimiento postoperatorio del hospital. "
    "Soy un asistente automatizado, no una persona, y esta llamada es para ver cómo "
    "va su recuperación. Si en algún momento prefiere hablar con alguien del equipo, "
    "dígamelo y le paso el aviso. ¿Hablo con {nombre}?"
)
"""Primera intervención, literal y siempre la misma.

Cumple la decisión 4 del contrato y el artículo 50 del AI Act en la primera
frase, antes de preguntar nada: se identifica el servicio, se dice «asistente
automatizado, no una persona» y se ofrece la salida humana. Termina preguntando
por el nombre, que es el primer paso de la verificación de identidad.

Se descartó generarlo con el modelo aunque el prompt lo describiera bien: no
había forma de garantizar la frase ni de demostrarla después."""

FRASE_SEGURIDAD = (
    "Perdone, no le he entendido bien. Voy a avisar a su equipo médico para que le "
    "llamen y lo revisen con usted."
)
"""Lo que se dice cuando el LLM falla, tarda de más o devuelve vacío.

Callar no es una opción por teléfono —un silencio se interpreta como que se ha
cortado— y improvisar con un modelo que acaba de fallar tampoco. Esta frase
cierra por el lado seguro: reconoce el problema y escala."""


_REGLAS = """\
REGLAS QUE NO PUEDES SALTARTE:
- Nunca diagnostiques. No digas qué le pasa ni qué puede ser.
- Nunca ajustes medicación: ni dosis, ni horarios, ni suspender, ni añadir nada.
  Puedes recordarle su pauta tal como está en el sistema, nada más.
- Toda indicación clínica sale de buscar_protocolo. Si no hay evidencia, di
  literalmente que no tienes esa información y ofrece escalar. No uses lo que
  sepas de medicina por tu cuenta: aquí eso es inventar.
- Nunca le digas su número de cédula ni ningún otro dato personal antes de que
  la persona se identifique. El número de cédula se lo pides, no se lo lees.
- Si el paciente pide hablar con una persona, escala y despídete. No insistas.
- Si dice algo que no entiendes, pregúntaselo otra vez. No supongas."""


_ESTILO = """\
CÓMO HABLAS:
- Esto es una llamada de teléfono y lo que escribas se convierte en voz. Frases
  cortas, español natural de conversación, sin listas, sin viñetas, sin emoji y
  sin nada que se lea distinto a como suena.
- Una sola pregunta por intervención. Dos preguntas seguidas por teléfono hacen
  que el paciente conteste solo a la última.
- Dos o tres frases como mucho. Si necesitas más, es que estás explicando de más.
- Los números, en palabras: «treinta y ocho y medio», no «38,5».
- Háblele siempre de usted."""


_FASES = """\
CÓMO VA LA LLAMADA:
1. Ya te has presentado y has preguntado si hablas con la persona correcta.
2. VERIFICA LA IDENTIDAD antes de nada clínico: llama a obtener_paciente para confirmar su
   nombre, pídele su número de cédula (o documento) y llama a verificar_identidad con el número
   que te diga. Si verificar_identidad devuelve coincide=false, o si quien contesta es otra
   persona, no des ningún dato: di que volverás a llamar en otro momento y despídete.
3. Cuando esté verificado, llama a obtener_cirugia y dile de qué le llamas y
   cuántos días han pasado desde la operación.
4. Recorre el guion de abajo. Registra cada respuesta con registrar_respuesta en
   cuanto la tengas.
5. Al terminar, resume en una frase lo que has anotado, recuérdale su próxima
   cita si la tiene, y despídete."""


def _cabecera(ctx: ContextoLlamada) -> str:
    # «a quien operaron», y no «operado» ni «operada»: el sexo del paciente no
    # está en la tabla `patients` y un prompt que lo asume acaba tratando de «él»
    # a una paciente en la primera frase de la llamada. La forma impersonal
    # esquiva la concordancia sin tener que adivinar nada.
    return (
        "Eres el asistente de seguimiento postoperatorio de un hospital. Llamas por "
        f"teléfono a {ctx.nombre_preferido}, a quien operaron hace {ctx.dias_postop} "
        f"días de {ctx.procedimiento}. Tu trabajo es saber cómo va su recuperación, resolver "
        "dudas leyendo el protocolo del hospital, y avisar a su equipo médico si algo "
        "no va bien. No eres su médico y no tomas decisiones clínicas."
    )


def sistema(ctx: ContextoLlamada) -> str:
    """El prompt de sistema completo para esta llamada."""
    preguntas = guion_para(ctx.protocol_tag, ctx.procedimiento)
    return "\n\n".join(
        [
            _cabecera(ctx),
            _ESTILO,
            _REGLAS,
            _FASES,
            "GUION DE ESTA LLAMADA (el corchete es la clave de registrar_respuesta):\n"
            + como_texto(preguntas),
            "No hace falta que sigas el orden a rajatabla: si el paciente se adelanta "
            "y contesta a algo, regístralo y no se lo vuelvas a preguntar.",
        ]
    )


BANDERA_ROJA = """\
ALARMA DETECTADA — «{motivo}».
Deja el guion aquí mismo. No preguntes nada más de la lista.
Haz esto, en este orden:
1. Llama a buscar_protocolo para saber qué hay que indicarle.
2. Dile en una frase qué has detectado y dale la indicación del protocolo, tal
   como venga. Si el protocolo no dice nada, dile que su equipo le va a llamar.
3. Llama a escalar_a_equipo_clinico con el motivo y la urgencia «{urgencia}».
4. Pregúntale si le ha quedado claro lo que tiene que hacer, y espera a que te
   conteste antes de despedirte."""
"""Se inyecta como mensaje de sistema en el turno en que salta el detector.

Va como mensaje del turno y no en el prompt permanente porque describe qué hacer
AHORA. En el prompt fijo sería una instrucción condicional que el modelo tiene
que evaluar en cada turno —y a veces evalúa mal—; inyectada, es una orden que
solo existe cuando corresponde.

Quién decide que hay alarma es `app/agent/redflags.py`, con expresiones
regulares. El modelo no clasifica: solo redacta lo que ya se ha decidido."""


CONFIRMACION_ENTENDIDO = """\
El paciente ya ha contestado a si lo ha entendido. Si te ha dicho que sí,
despídete en una frase. Si te ha dicho que no o no ha quedado claro, repítele la
indicación con otras palabras, más despacio, y vuelve a preguntar."""
