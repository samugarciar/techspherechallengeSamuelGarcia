/**
 * Tipos derivados de `docs/CONTRATO_LLAMADAS.md`.
 *
 * Mismo criterio que `types/api.ts`: traducción literal del documento, sin
 * inventar alias. Donde el contrato deja un hueco —y deja varios— el tipo es
 * deliberadamente permisivo (campo opcional o `| null`) y la normalización vive
 * en `api/llamadas/normalizar.ts`. Un backend en obras que devuelve un campo de
 * menos no debe dejar la pantalla en blanco en mitad de la demo.
 */

// ---------------------------------------------------------------------------
// Pacientes — GET /api/patients
// ---------------------------------------------------------------------------

export interface CirugiaResumen {
  nombre: string
  /** ISO `YYYY-MM-DD`. */
  fecha: string | null
  /** Días transcurridos desde la operación. Es la cifra que decide el guion. */
  dias_desde: number | null
}

export interface Paciente {
  id: string
  nombre: string
  /** Nombre por el que se le llama en voz alta. Puede faltar. */
  preferred_name: string | null
  documento_cc: string | null
  cirugia: CirugiaResumen | null
  medicacion_activa: number
  proxima_cita: string | null
  /** ISO de la última llamada, o `null` si nunca se le ha llamado. */
  ultima_llamada: string | null
}

export interface ListaPacientes {
  pacientes: Paciente[]
}

// ---------------------------------------------------------------------------
// Llamadas — POST /api/calls, GET /api/calls, GET /api/calls/{id}
// ---------------------------------------------------------------------------

export interface LlamadaCreada {
  call_id: string
  /** Ruta relativa del WebSocket, p. ej. `/ws/voz?call_id=…`. */
  ws: string
}

export type EstadoLlamada = 'en_curso' | 'completada' | 'interrumpida'

export interface ResumenLlamada {
  id: string
  paciente: string
  cirugia: string | null
  iniciada: string
  duracion_s: number | null
  estado: EstadoLlamada
  escalada: boolean
  motivo_escalada: string | null
  /** Número de turnos. En el detalle, `turnos` es una lista; ver `normalizar.ts`. */
  turnos: number
}

export interface ListaLlamadas {
  llamadas: ResumenLlamada[]
}

/** Una cita es la unidad de auditoría: de aquí salió esta frase del agente. */
export interface Cita {
  filename: string
  heading: string | null
  page: number | null
}

export type QuienHabla = 'agente' | 'paciente' | 'sistema'

/**
 * Latencias por etapa, en milisegundos.
 *
 * El contrato de llamadas nombra `stt`, `llm` y `tts` en los ejemplos y menciona
 * `retrieval` en la descripción del panel. Todas son opcionales porque un turno
 * del paciente sólo tiene `stt` y uno del agente no tiene `stt` en absoluto.
 */
export interface EtapasLlamadaMs {
  stt?: number
  retrieval?: number
  llm?: number
  tts?: number
  total?: number
}

/** Las cuatro etapas que pinta el panel, en el orden real del turno. */
export const ETAPAS_LATENCIA = ['stt', 'retrieval', 'llm', 'tts'] as const
export type EtapaLatencia = (typeof ETAPAS_LATENCIA)[number]

export const NOMBRE_ETAPA: Record<EtapaLatencia, string> = {
  stt: 'Transcripción',
  retrieval: 'Búsqueda',
  llm: 'Razonamiento',
  tts: 'Voz',
}

export const DETALLE_ETAPA: Record<EtapaLatencia, string> = {
  stt: 'Whisper convierte en texto lo que dijo el paciente.',
  retrieval: 'Búsqueda de evidencia en los protocolos indexados.',
  llm: 'Gemini 2.5 Flash redacta la respuesta con esa evidencia.',
  tts: 'Síntesis de la primera frase; el resto sale ya sonando.',
}

export interface TurnoLlamada {
  ordinal: number
  quien: QuienHabla
  texto: string
  citas: Cita[]
  ms: EtapasLlamadaMs
}

export interface LlamadaDetalle extends Omit<ResumenLlamada, 'turnos'> {
  turnos: TurnoLlamada[]
}

// ---------------------------------------------------------------------------
// Protocolo del WebSocket /ws/voz
// ---------------------------------------------------------------------------

export type FaseLlamada = 'escuchando' | 'pensando' | 'hablando'

export type MotivoFin = 'completada' | 'escalada' | 'cortada'

export type UrgenciaBandera = 'rutina' | 'prioritaria' | 'urgente'

/**
 * Mensajes de control del servidor.
 *
 * Las siete primeras variantes son las de `CONTRATO_LLAMADAS.md`. Las cinco
 * últimas son las que emite HOY el bucle de voz que ya corre
 * (`app/voice/pipeline_ws.py`, documentadas en `CONTRATO_API.md`): mientras el
 * backend nuevo no esté montado, la pantalla tiene que entenderse con el que
 * existe, y el día que cambie no habrá que tocar nada. `parar` **no está en la
 * tabla del contrato de llamadas y es obligatorio**: es el único mensaje que
 * vacía el buffer del cliente en un barge-in. Anotado en §Cambios.
 */
export type MensajeVoz =
  | { tipo: 'listo'; sample_rate_entrada?: number; sample_rate_salida?: number }
  | { tipo: 'estado'; fase: FaseLlamada }
  | { tipo: 'transcripcion'; quien?: QuienHabla; texto: string; parcial?: boolean }
  | { tipo: 'citas'; citas: Cita[] }
  | { tipo: 'bandera_roja'; motivo: string; urgencia?: UrgenciaBandera }
  | { tipo: 'metricas'; ms: EtapasLlamadaMs }
  | { tipo: 'fin'; motivo: MotivoFin }
  | { tipo: 'paciente_habla' }
  | { tipo: 'fin_de_turno' }
  | { tipo: 'agente_habla'; texto?: string }
  | { tipo: 'parar' }
  | { tipo: 'fin_audio'; texto?: string }

/** Etiquetas de la fase. Deliberadamente cortas: se leen desde el fondo de la sala. */
export const ETIQUETA_FASE: Record<FaseLlamada, string> = {
  escuchando: 'Escuchando',
  pensando: 'Pensando',
  hablando: 'Hablando',
}

export const ETIQUETA_ESTADO_LLAMADA: Record<EstadoLlamada, string> = {
  en_curso: 'En curso',
  completada: 'Completada',
  interrumpida: 'Interrumpida',
}

export const ETIQUETA_URGENCIA: Record<UrgenciaBandera, string> = {
  rutina: 'Rutina',
  prioritaria: 'Prioritaria',
  urgente: 'Urgente',
}

export const ETIQUETA_MOTIVO_FIN: Record<MotivoFin, string> = {
  completada: 'Seguimiento completado',
  escalada: 'Escalada al equipo médico',
  cortada: 'Llamada cortada',
}
