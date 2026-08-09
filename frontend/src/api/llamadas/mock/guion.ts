import type { Cita, Paciente, UrgenciaBandera } from '@/types/llamadas'

/**
 * ===========================================================================
 *  GUIONES DEL SIMULADOR — NO ENTRA EN PRODUCCIÓN
 * ===========================================================================
 *
 * El contenido de la llamada simulada. Está separado del runner porque es lo
 * único que se toca para ensayar la demo: cambiar una frase no debería obligar a
 * releer la máquina de estados.
 *
 * Respeta las decisiones de producto del contrato, y por eso sirve de ensayo
 * real y no de decorado:
 *   · el agente se presenta como sistema automatizado (decisión 4, AI Act);
 *   · verifica identidad con nombre y fecha de nacimiento contra los datos de la
 *     ficha antes de entrar en materia clínica (decisión 3);
 *   · el bloque de preguntas es común y la última es específica de la cirugía
 *     (decisión 1);
 *   · ante bandera roja abandona lo que quedaba, da la instrucción de urgencia,
 *     CONFIRMA que la paciente la ha entendido y cierra (decisión 2).
 */

export type PasoGuion =
  | {
      clase: 'agente'
      texto: string
      citas?: Cita[]
      /** Segundos que se «tarda» en decirlo. Si falta, se estima por longitud. */
      duracion?: number
    }
  | { clase: 'paciente'; texto: string; espera?: number }
  | { clase: 'bandera'; motivo: string; urgencia: UrgenciaBandera }
  | { clase: 'fin'; motivo: 'completada' | 'escalada' | 'cortada' }

export type ModoGuion = 'con_bandera' | 'sin_bandera'

const PROTOCOLO = 'protocolo_alta_apendicectomia.pdf'
const HERIDA = 'cuidados_herida_quirurgica.md'
const ANALGESIA = 'pauta_analgesia_postoperatoria.docx'

function cita(filename: string, heading: string, page: number | null): Cita {
  return { filename, heading, page }
}

/** El nombre corto es como se dirige a la persona en voz alta. */
function nombreCorto(paciente: Paciente): string {
  return paciente.preferred_name ?? paciente.nombre.split(' ')[0] ?? 'Buenos días'
}

function apellidos(paciente: Paciente): string {
  const partes = paciente.nombre.split(' ')
  return partes.slice(0, 3).join(' ')
}

function diasTexto(paciente: Paciente): string {
  const dias = paciente.cirugia?.dias_desde ?? 0
  if (dias <= 1) return 'Ayer'
  return `Hace ${dias} días`
}

function fechaEnLetra(iso: string | null): string {
  if (!iso) return 'no consta'
  const [ano, mes, dia] = iso.split('-')
  const meses = [
    'enero',
    'febrero',
    'marzo',
    'abril',
    'mayo',
    'junio',
    'julio',
    'agosto',
    'septiembre',
    'octubre',
    'noviembre',
    'diciembre',
  ]
  const indice = Number(mes) - 1
  return `${Number(dia)} de ${meses[indice] ?? mes} de ${ano}`
}

/**
 * Pregunta específica del procedimiento. Es la mitad visible de la decisión 1:
 * a un colecistectomizado se le pregunta por las grasas y a un herniorrafiado
 * por los esfuerzos, y eso no sale de un guion único.
 */
function preguntaEspecifica(paciente: Paciente): { pregunta: string; respuesta: string } {
  const cirugia = (paciente.cirugia?.nombre ?? '').toLowerCase()
  if (cirugia.includes('colecist')) {
    return {
      pregunta:
        'Una última cosa, propia de su cirugía: ¿cómo ha tolerado las comidas con grasa? ' +
        '¿Ha tenido náuseas o diarrea después de comer?',
      respuesta: 'Al principio me sentaba fatal, pero estos dos últimos días ya como normal.',
    }
  }
  if (cirugia.includes('hernio') || cirugia.includes('hernia')) {
    return {
      pregunta:
        'Una última cosa, propia de su cirugía: ¿ha levantado peso o hecho algún esfuerzo? ' +
        'Toser o agacharse a coger algo del suelo también cuenta.',
      respuesta: 'Cogí una bolsa de la compra el otro día, pero no pesaba mucho.',
    }
  }
  return {
    pregunta:
      'Una última cosa, propia de su cirugía: ¿ha vuelto a hacer deposición con normalidad y ' +
      'tolera bien la comida?',
    respuesta: 'Sí, ayer ya comí normal y fui al baño sin problema.',
  }
}

/** Apertura común: presentación, aviso de sistema automatizado y verificación. */
function apertura(paciente: Paciente): PasoGuion[] {
  return [
    {
      clase: 'agente',
      texto:
        `Buenos días. Le llamo del servicio de cirugía para el seguimiento de su operación. ` +
        `Antes de nada le informo de que soy un sistema automatizado, no una persona: la ` +
        `conversación queda registrada y la revisa su equipo médico. ¿Hablo con ` +
        `${apellidos(paciente)}?`,
    },
    { clase: 'paciente', texto: 'Sí, soy yo.', espera: 1.1 },
    {
      clase: 'agente',
      texto: 'Gracias. Para confirmar su identidad, ¿me dice su fecha de nacimiento?',
    },
    {
      clase: 'paciente',
      texto: fechaEnLetra(paciente.fecha_nacimiento) + '.',
      espera: 1.4,
    },
  ]
}

/** Guion completo con bandera roja a mitad. Es el que se enseña al jurado. */
function conBandera(paciente: Paciente): PasoGuion[] {
  const nombre = nombreCorto(paciente)
  const cirugia = paciente.cirugia?.nombre.toLowerCase() ?? 'su operación'

  return [
    ...apertura(paciente),
    {
      clase: 'agente',
      texto:
        `Correcto, gracias. ${diasTexto(paciente)} le hicieron una ${cirugia}. ` +
        `Voy a hacerle unas preguntas breves sobre cómo va la recuperación. ` +
        `Empecemos por el dolor: del cero al diez, ¿cómo lo tiene hoy?`,
      citas: [cita(PROTOCOLO, 'Seguimiento telefónico a las 72 horas', 2)],
    },
    {
      clase: 'paciente',
      texto: 'Pues ayer lo tenía en un cuatro, pero hoy me ha subido bastante. Como un siete.',
      espera: 1.6,
    },
    {
      clase: 'agente',
      texto:
        'Anotado, un siete. Un dolor que aumenta al tercer día en vez de bajar conviene ' +
        'mirarlo con calma. ¿Ha tenido fiebre? ¿Se ha tomado la temperatura hoy?',
      citas: [
        cita(ANALGESIA, 'Evolución esperada del dolor', 2),
        cita(PROTOCOLO, 'Signos de alarma', 4),
      ],
    },
    {
      clase: 'paciente',
      texto: 'Sí, me la he tomado hace un rato y tenía treinta y nueve y dos.',
      espera: 1.5,
    },
    {
      clase: 'bandera',
      motivo: 'Fiebre de 39,2 °C al tercer día, con dolor en aumento',
      urgencia: 'urgente',
    },
    {
      clase: 'agente',
      texto:
        `${nombre}, escúcheme bien porque esto es importante. Una fiebre de treinta y nueve ` +
        `con dos junto con un dolor que va a más no es parte de la recuperación normal: hay ` +
        `que valorarla hoy mismo. No espere a la cita de revisión. Acuda ahora al servicio de ` +
        `urgencias del hospital donde la operaron, y si no puede desplazarse por su cuenta, ` +
        `llame a emergencias. No se tome otra dosis de analgésico hasta que la valoren, ` +
        `porque puede bajar la fiebre y esconder lo que está pasando. ` +
        `Dígame, ¿qué es lo que va a hacer ahora?`,
      citas: [
        cita(PROTOCOLO, 'Signos de alarma', 4),
        cita(PROTOCOLO, 'Cuándo acudir a urgencias', 5),
        cita(HERIDA, 'Infección de la herida quirúrgica', null),
      ],
    },
    {
      clase: 'paciente',
      texto: 'Que vaya a urgencias ahora mismo y que no me tome nada más.',
      espera: 1.8,
    },
    {
      clase: 'agente',
      texto:
        `Exacto, eso es. Dejo constancia de esta llamada y aviso ahora mismo a su equipo ` +
        `médico para que sepan que va usted en camino. Vaya sin esperar, ${nombre}. Cuídese.`,
    },
    { clase: 'fin', motivo: 'escalada' },
  ]
}

/** Guion sin incidencias: sirve para ensayar el cierre normal y el resumen. */
function sinBandera(paciente: Paciente): PasoGuion[] {
  const nombre = nombreCorto(paciente)
  const cirugia = paciente.cirugia?.nombre.toLowerCase() ?? 'su operación'
  const especifica = preguntaEspecifica(paciente)
  const medicacion = paciente.medicacion_activa

  return [
    ...apertura(paciente),
    {
      clase: 'agente',
      texto:
        `Correcto, gracias. ${diasTexto(paciente)} le hicieron una ${cirugia}. ` +
        `Voy a hacerle unas preguntas breves. Del cero al diez, ¿cómo tiene hoy el dolor?`,
      citas: [cita(PROTOCOLO, 'Seguimiento telefónico a las 72 horas', 2)],
    },
    { clase: 'paciente', texto: 'Bien, la verdad. Un dos, sobre todo al levantarme.', espera: 1.4 },
    {
      clase: 'agente',
      texto:
        'Me alegro, un dos a estas alturas es justo lo esperable. ¿Y la herida? ' +
        '¿La ve enrojecida, hinchada, o le sale algún líquido?',
      citas: [cita(HERIDA, 'Aspecto normal de la herida', null)],
    },
    {
      clase: 'paciente',
      texto: 'No, está limpia. Un poco morada alrededor, pero nada más.',
      espera: 1.5,
    },
    {
      clase: 'agente',
      texto:
        'Ese hematoma alrededor es normal y se irá solo en una o dos semanas. ' +
        '¿Ha tenido fiebre en algún momento?',
      citas: [cita(HERIDA, 'Hematoma y equimosis', null)],
    },
    { clase: 'paciente', texto: 'No, me la he ido tomando y nada, treinta y seis y medio.', espera: 1.2 },
    {
      clase: 'agente',
      texto:
        medicacion > 0
          ? `Perfecto. Tiene ${medicacion} medicamentos pautados ahora mismo. ` +
            `¿Los está tomando a las horas que le indicaron?`
          : 'Perfecto. ¿Está tomando alguna medicación por su cuenta?',
      citas: [cita(ANALGESIA, 'Pauta domiciliaria', 1)],
    },
    {
      clase: 'paciente',
      texto: 'Sí, aunque anoche se me pasó una toma y la hice al levantarme.',
      espera: 1.7,
    },
    {
      clase: 'agente',
      texto:
        'No pasa nada, eso es lo correcto: se retoma la pauta desde ahí, sin doblar la dosis. ' +
        especifica.pregunta,
      citas: [cita(ANALGESIA, 'Olvido de una dosis', 3)],
    },
    { clase: 'paciente', texto: especifica.respuesta, espera: 1.5 },
    {
      clase: 'agente',
      texto:
        'Muy bien. Por mi parte ya está todo. ¿Tiene usted alguna duda que quiera dejar ' +
        'anotada para su equipo?',
    },
    { clase: 'paciente', texto: 'No, ninguna. Gracias.', espera: 1.1 },
    {
      clase: 'agente',
      texto:
        `Pues nada más, ${nombre}. Queda registrado que la recuperación va bien. ` +
        `Si aparece fiebre, el dolor sube o la herida cambia de aspecto, no espere a la ` +
        `revisión: contacte con el hospital. Que se mejore.`,
      citas: [cita(PROTOCOLO, 'Signos de alarma', 4)],
    },
    { clase: 'fin', motivo: 'completada' },
  ]
}

export function construirGuion(paciente: Paciente, modo: ModoGuion): PasoGuion[] {
  return modo === 'con_bandera' ? conBandera(paciente) : sinBandera(paciente)
}
