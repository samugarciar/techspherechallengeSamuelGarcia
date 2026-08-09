/**
 * Corpus clínico del simulador.
 *
 * No es relleno: son secciones plausibles de un alta postoperatoria, con el
 * vocabulario que de verdad usan las preguntas de un paciente («¿me puedo
 * duchar?», «¿cuánta fiebre es mucha?»). Eso hace que la caja de consulta al RAG
 * simulada devuelva algo defendible en pantalla y no un lorem ipsum.
 *
 * Los textos son sintéticos y NO son consejo médico; existen para poder enseñar
 * la mecánica de aprender y olvidar sin depender del backend.
 */

export interface SeccionCorpus {
  heading: string
  content: string
}

export const CORPUS: SeccionCorpus[] = [
  {
    heading: 'Cuidado de la herida',
    content:
      'Mantenga la herida limpia y seca durante las primeras 48 horas. El apósito puede retirarse a partir del segundo día si no está manchado. Lave la zona con agua y jabón neutro, sin frotar, y séquela con una toalla limpia dando pequeños toques. No aplique cremas, alcohol ni povidona yodada sobre los puntos salvo indicación expresa del equipo quirúrgico.',
  },
  {
    heading: 'Signos de alarma',
    content:
      'Acuda a urgencias si presenta fiebre superior a 38,5 grados, enrojecimiento creciente alrededor de la herida, salida de pus o líquido maloliente, dolor abdominal intenso que no cede con la analgesia pautada, vómitos persistentes durante más de doce horas o ausencia de deposiciones y gases más allá de tres días tras la intervención.',
  },
  {
    heading: 'Medicación y analgesia',
    content:
      'Paracetamol 1 gramo cada 8 horas de forma pautada durante los primeros cinco días, no a demanda. Si el dolor persiste, añada ibuprofeno 600 miligramos cada 8 horas alternando con el paracetamol, siempre con el estómago lleno. La cefalexina 500 miligramos cada 8 horas durante 7 días debe completarse aunque se encuentre bien antes de terminar el envase.',
  },
  {
    heading: 'Higiene y ducha',
    content:
      'Puede ducharse a partir de las 48 horas de la intervención, siempre con la herida cubierta si aún lleva apósito. Evite el baño de inmersión, la piscina, el mar y el jacuzzi hasta que hayan retirado los puntos y la herida esté completamente cerrada, habitualmente entre diez y catorce días después de la cirugía.',
  },
  {
    heading: 'Alimentación',
    content:
      'Reintroduzca la alimentación de forma progresiva: el primer día líquidos claros y dieta blanda, y a partir del segundo día alimentación normal según tolerancia. Beba entre litro y medio y dos litros de agua al día. Evite comidas copiosas, fritos y alcohol durante la primera semana, sobre todo si la cirugía ha sido de vesícula.',
  },
  {
    heading: 'Actividad física y reposo',
    content:
      'Camine desde el mismo día de la intervención, en trayectos cortos y frecuentes: la movilización precoz previene trombosis y ayuda al tránsito intestinal. No levante pesos superiores a cinco kilos durante cuatro semanas, ni conduzca mientras tome analgesia con efecto sedante o el cinturón le provoque dolor sobre la herida.',
  },
  {
    heading: 'Cita de revisión',
    content:
      'La revisión en consulta de cirugía se realiza entre los siete y los diez días posteriores al alta, y en ella se valorará la retirada de puntos o grapas. Si la cita no aparece en su documentación de alta, llame al teléfono de secretaría del servicio en horario de mañana para concretarla; no espere a que le llamen.',
  },
  {
    heading: 'Cuándo llamar al equipo',
    content:
      'Llame al teléfono de contacto del servicio de cirugía si tiene dudas sobre la medicación, si la herida se abre parcialmente, si aparece sangrado que empapa el apósito o si no consigue controlar el dolor con la pauta indicada. Fuera del horario de consulta, el circuito de referencia es el servicio de urgencias del hospital.',
  },
  {
    heading: 'Drenajes y apósitos',
    content:
      'Si ha sido dado de alta con drenaje, anote a diario la cantidad y el color del líquido recogido y llévelo apuntado a la revisión. Cambie el apósito cuando esté manchado o húmedo, siempre con las manos lavadas y material limpio. No traccione del drenaje ni intente retirarlo por su cuenta bajo ninguna circunstancia.',
  },
  {
    heading: 'Fiebre y control de temperatura',
    content:
      'Es habitual una febrícula de hasta 37,5 grados en las primeras 48 horas. Tómese la temperatura dos veces al día durante la primera semana y anótela. Una temperatura de 38 grados o más mantenida durante más de ocho horas, o cualquier pico por encima de 38,5 grados, obliga a contactar con el equipo quirúrgico el mismo día.',
  },
]

/** Palabras vacías del español: sin filtrarlas, «el» y «de» dominan la puntuación. */
export const VACIAS = new Set(
  `a al algo alguna algunas alguno algunos ante antes aqui como con contra cual cuales cuando de del desde donde dos el ella ellas ellos en entre era eran es esa esas ese eso esos esta estan estas este esto estos ha hace hacia han hasta hay la las le les lo los mas me mi mis mucho muy nada ni no nos o os otra otro para pero poco por porque que quien se ser si sin sobre solo son su sus tan te tiene todo todos tu tus un una uno unos y ya yo`.split(
    ' ',
  ),
)

/** Minúsculas y sin tildes: «medicacion» debe casar con «medicación». */
export function normalizar(texto: string): string {
  return texto
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
}

/**
 * Raíz aproximada de una palabra: los cinco primeros caracteres a partir de seis
 * de longitud.
 *
 * Es un stemmer de pobres, y aun así imprescindible: el paciente pregunta
 * «¿cuándo puedo ducharme?» y el protocolo dice «puede ducharse». Sin recortar,
 * el simulador no encontraría nada y la demo del RAG parecería rota por un
 * detalle morfológico que el backend real —denso + FTS en español— resuelve solo.
 */
export function raiz(palabra: string): string {
  return palabra.length >= 6 ? palabra.slice(0, 5) : palabra
}

export function terminos(texto: string): string[] {
  return normalizar(texto)
    .split(/[^a-z0-9]+/)
    .filter((palabra) => palabra.length >= 3 && !VACIAS.has(palabra))
    .map(raiz)
}
