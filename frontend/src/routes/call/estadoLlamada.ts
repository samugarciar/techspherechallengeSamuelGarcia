import type {
  Cita,
  EtapasLlamadaMs,
  FaseLlamada,
  MensajeVoz,
  MotivoFin,
  QuienHabla,
  UrgenciaBandera,
} from '@/types/llamadas'

/**
 * Estado de una llamada en curso, y el reductor que lo mueve a golpe de mensaje
 * del WebSocket.
 *
 * Está separado del hook a propósito: es lógica pura sobre el protocolo del
 * contrato —incluida la parte incómoda de consolidar parciales— y aquí se puede
 * leer y razonar sin AudioContext, sin React y sin red por medio.
 */

export interface Intervencion {
  clave: string
  quien: QuienHabla
  texto: string
  /** Mientras es `true`, el STT todavía puede cambiar lo dicho. */
  parcial: boolean
  citas: Cita[]
  ms: EtapasLlamadaMs | null
  /** ms desde el inicio de la llamada; sirve para el sello de tiempo. */
  instante: number
}

export interface CitaUsada extends Cita {
  clave: string
  instante: number
}

export interface BanderaRoja {
  motivo: string
  urgencia: UrgenciaBandera
  instante: number
}

export interface EstadoConversacion {
  fase: FaseLlamada | null
  intervenciones: Intervencion[]
  /** Citas de toda la llamada, en orden de uso. */
  citas: CitaUsada[]
  /** Citas anunciadas que aún no tienen a qué respuesta pegarse. */
  pendientes: Cita[]
  bandera: BanderaRoja | null
  metricas: EtapasLlamadaMs[]
  fin: MotivoFin | null
  /** Barge-ins detectados y ms de voz del agente descartados por ellos. */
  interrupciones: number
  msDescartados: number
}

export const ESTADO_INICIAL: EstadoConversacion = {
  fase: null,
  intervenciones: [],
  citas: [],
  pendientes: [],
  bandera: null,
  metricas: [],
  fin: null,
  interrupciones: 0,
  msDescartados: 0,
}

export type AccionLlamada =
  | { tipo: 'reiniciar' }
  | { tipo: 'mensaje'; mensaje: MensajeVoz; instante: number }
  | { tipo: 'interrupcion'; msDescartados: number }
  | { tipo: 'nota'; texto: string; instante: number }
  | { tipo: 'terminar'; motivo: MotivoFin }

let contador = 0
function nuevaClave(prefijo: string): string {
  contador += 1
  return `${prefijo}-${contador}`
}

function mismaCita(a: Cita, b: Cita): boolean {
  return a.filename === b.filename && a.heading === b.heading && a.page === b.page
}

/**
 * Añade texto a la intervención abierta de quien habla, o abre una nueva.
 *
 * La regla que importa: una intervención parcial se SUSTITUYE, no se concatena.
 * El STT reenvía la frase entera cada vez que la revisa, así que concatenar
 * produciría «me duele me duele mucho me duele mucho la herida».
 */
function aplicarTranscripcion(
  estado: EstadoConversacion,
  quien: QuienHabla,
  texto: string,
  parcial: boolean,
  instante: number,
): EstadoConversacion {
  const intervenciones = [...estado.intervenciones]
  const ultima = intervenciones[intervenciones.length - 1]

  if (ultima && ultima.quien === quien && ultima.parcial) {
    intervenciones[intervenciones.length - 1] = { ...ultima, texto, parcial }
    return { ...estado, intervenciones }
  }

  // Una intervención nueva del agente se queda con las citas que se anunciaron
  // justo antes: el contrato manda `citas` pegado a la respuesta que fundamentan.
  const citas = quien === 'agente' ? estado.pendientes : []
  intervenciones.push({
    clave: nuevaClave(quien),
    quien,
    texto,
    parcial,
    citas,
    ms: null,
    instante,
  })
  return {
    ...estado,
    intervenciones,
    pendientes: quien === 'agente' ? [] : estado.pendientes,
  }
}

function adjuntarMetricas(
  estado: EstadoConversacion,
  ms: EtapasLlamadaMs,
): EstadoConversacion {
  const intervenciones = [...estado.intervenciones]
  const indice = intervenciones.length - 1
  const ultima = intervenciones[indice]
  if (ultima) intervenciones[indice] = { ...ultima, ms: { ...(ultima.ms ?? {}), ...ms } }
  return { ...estado, intervenciones, metricas: [...estado.metricas, ms] }
}

function adjuntarCitas(estado: EstadoConversacion, citas: Cita[], instante: number): EstadoConversacion {
  if (citas.length === 0) return estado

  const nuevas = citas
    .filter((cita) => !estado.citas.some((usada) => mismaCita(usada, cita)))
    .map((cita) => ({ ...cita, clave: nuevaClave('cita'), instante }))

  const intervenciones = [...estado.intervenciones]
  const indice = intervenciones.length - 1
  const ultima = intervenciones[indice]

  // Si el agente ya está hablando, las citas son de ESA respuesta y se pegan
  // ahí mismo. Si todavía no ha abierto la boca, esperan en `pendientes`.
  if (ultima && ultima.quien === 'agente' && ultima.parcial) {
    intervenciones[indice] = {
      ...ultima,
      citas: [...ultima.citas, ...citas.filter((c) => !ultima.citas.some((y) => mismaCita(y, c)))],
    }
    return { ...estado, intervenciones, citas: [...estado.citas, ...nuevas] }
  }

  return { ...estado, pendientes: citas, citas: [...estado.citas, ...nuevas] }
}

export function reductorLlamada(
  estado: EstadoConversacion,
  accion: AccionLlamada,
): EstadoConversacion {
  switch (accion.tipo) {
    case 'reiniciar':
      return ESTADO_INICIAL

    case 'interrupcion':
      return {
        ...estado,
        interrupciones: estado.interrupciones + 1,
        msDescartados: estado.msDescartados + accion.msDescartados,
      }

    case 'nota': {
      const intervenciones = [...estado.intervenciones]
      // Cierra lo que estuviera abierto: una nota del sistema no debe quedar
      // dentro del turno de nadie.
      const indice = intervenciones.length - 1
      const ultima = intervenciones[indice]
      if (ultima?.parcial) intervenciones[indice] = { ...ultima, parcial: false }
      intervenciones.push({
        clave: nuevaClave('sistema'),
        quien: 'sistema',
        texto: accion.texto,
        parcial: false,
        citas: [],
        ms: null,
        instante: accion.instante,
      })
      return { ...estado, intervenciones }
    }

    case 'terminar':
      return { ...estado, fin: estado.fin ?? accion.motivo, fase: null }

    case 'mensaje': {
      const { mensaje, instante } = accion
      switch (mensaje.tipo) {
        case 'listo':
          return estado

        case 'estado':
          return { ...estado, fase: mensaje.fase }

        case 'transcripcion':
          return aplicarTranscripcion(
            estado,
            // El bucle que corre hoy no manda `quien`: sólo transcribe al
            // paciente, así que ese es el valor por defecto correcto.
            mensaje.quien ?? 'paciente',
            mensaje.texto,
            mensaje.parcial ?? false,
            instante,
          )

        case 'citas':
          return adjuntarCitas(estado, mensaje.citas ?? [], instante)

        case 'bandera_roja':
          return {
            ...estado,
            bandera: {
              motivo: mensaje.motivo,
              urgencia: mensaje.urgencia ?? 'urgente',
              instante,
            },
          }

        case 'metricas':
          return adjuntarMetricas(estado, mensaje.ms ?? {})

        case 'fin':
          return { ...estado, fin: mensaje.motivo, fase: null }

        // --- protocolo del bucle de voz que ya corre en el servidor ---------
        case 'paciente_habla':
          return { ...estado, fase: 'escuchando' }

        case 'fin_de_turno':
          return { ...estado, fase: 'pensando' }

        case 'agente_habla':
          return mensaje.texto
            ? aplicarTranscripcion(
                { ...estado, fase: 'hablando' },
                'agente',
                mensaje.texto,
                true,
                instante,
              )
            : { ...estado, fase: 'hablando' }

        case 'fin_audio':
          return mensaje.texto
            ? aplicarTranscripcion(estado, 'agente', mensaje.texto, false, instante)
            : estado

        case 'parar':
          // El vaciado del buffer es un efecto y lo hace el hook; aquí sólo se
          // registra que el paciente tomó la palabra.
          return { ...estado, fase: 'escuchando' }

        default:
          return estado
      }
    }

    default:
      return estado
  }
}

// ---------------------------------------------------------------------------
// Derivados
// ---------------------------------------------------------------------------

/** Mediana de una etapa a lo largo de la llamada. Resiste mejor un turno raro. */
export function medianaEtapa(metricas: EtapasLlamadaMs[], etapa: keyof EtapasLlamadaMs): number | null {
  const valores = metricas
    .map((m) => m[etapa])
    .filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
    .sort((a, b) => a - b)
  if (valores.length === 0) return null
  const medio = Math.floor(valores.length / 2)
  if (valores.length % 2 === 1) return valores[medio] ?? null
  const izquierda = valores[medio - 1] ?? 0
  const derecha = valores[medio] ?? 0
  return (izquierda + derecha) / 2
}

/** La última métrica que trae algo de la etapa pedida. */
export function ultimaConEtapa(
  metricas: EtapasLlamadaMs[],
  etapa: keyof EtapasLlamadaMs,
): number | null {
  for (let i = metricas.length - 1; i >= 0; i -= 1) {
    const valor = metricas[i]?.[etapa]
    if (typeof valor === 'number' && Number.isFinite(valor)) return valor
  }
  return null
}
