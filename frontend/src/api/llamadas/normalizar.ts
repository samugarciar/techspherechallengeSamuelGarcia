import type {
  Cita,
  CirugiaResumen,
  EstadoLlamada,
  EtapasLlamadaMs,
  ListaLlamadas,
  ListaPacientes,
  LlamadaCreada,
  LlamadaDetalle,
  Paciente,
  QuienHabla,
  ResumenLlamada,
  TurnoLlamada,
} from '@/types/llamadas'

/**
 * Normalización de lo que llega por la red.
 *
 * Aquí no se «arregla» el backend: se le pone un cinturón. La pantalla de la
 * llamada es la principal de la demo y un campo que llegue `undefined` no puede
 * traducirse en una pantalla en blanco delante del jurado. Cada función devuelve
 * siempre un objeto completo y con tipos correctos, aunque el origen venga
 * incompleto.
 *
 * Hay un hueco real del contrato que se resuelve aquí: `turnos` es un NÚMERO en
 * `GET /api/calls` y una LISTA en `GET /api/calls/{id}`. Se aceptan los dos.
 */

function texto(valor: unknown, porDefecto = ''): string {
  return typeof valor === 'string' ? valor : porDefecto
}

function textoOpcional(valor: unknown): string | null {
  return typeof valor === 'string' && valor.trim() !== '' ? valor : null
}

function numero(valor: unknown, porDefecto = 0): number {
  return typeof valor === 'number' && Number.isFinite(valor) ? valor : porDefecto
}

function numeroOpcional(valor: unknown): number | null {
  return typeof valor === 'number' && Number.isFinite(valor) ? valor : null
}

function objeto(valor: unknown): Record<string, unknown> {
  return valor !== null && typeof valor === 'object' ? (valor as Record<string, unknown>) : {}
}

function lista(valor: unknown): unknown[] {
  return Array.isArray(valor) ? valor : []
}

// ---------------------------------------------------------------------------
// Pacientes
// ---------------------------------------------------------------------------

function normalizarCirugia(bruto: unknown): CirugiaResumen | null {
  const c = objeto(bruto)
  const nombre = texto(c.nombre ?? c.procedure_name)
  if (!nombre) return null
  return {
    nombre,
    fecha: textoOpcional(c.fecha ?? c.performed_at),
    dias_desde: numeroOpcional(c.dias_desde),
  }
}

export function normalizarPaciente(bruto: unknown): Paciente {
  const p = objeto(bruto)
  return {
    id: texto(p.id),
    // `full_name` es el nombre de la columna en el schema; si el backend lo
    // publica tal cual en vez de traducirlo, la pantalla no se queda muda.
    nombre: texto(p.nombre ?? p.full_name, 'Paciente sin nombre'),
    preferred_name: textoOpcional(p.preferred_name),
    fecha_nacimiento: textoOpcional(p.fecha_nacimiento ?? p.birth_date),
    cirugia: normalizarCirugia(p.cirugia),
    medicacion_activa: numero(p.medicacion_activa),
    proxima_cita: textoOpcional(p.proxima_cita),
    ultima_llamada: textoOpcional(p.ultima_llamada),
  }
}

export function normalizarListaPacientes(bruto: unknown): ListaPacientes {
  const datos = objeto(bruto)
  return {
    pacientes: lista(datos.pacientes)
      .map(normalizarPaciente)
      .filter((paciente) => paciente.id !== ''),
  }
}

// ---------------------------------------------------------------------------
// Llamadas
// ---------------------------------------------------------------------------

export function normalizarLlamadaCreada(bruto: unknown): LlamadaCreada {
  const datos = objeto(bruto)
  const id = texto(datos.call_id ?? datos.id)
  // El contrato promete `ws`, pero si faltara se puede reconstruir: es una ruta
  // fija más el `call_id`. Preferible a abortar la llamada por un campo de menos.
  const ws = texto(datos.ws) || `/ws/voz?call_id=${encodeURIComponent(id)}`
  return { call_id: id, ws }
}

const ESTADOS_VALIDOS: EstadoLlamada[] = ['en_curso', 'completada', 'interrumpida']

/**
 * El schema de la base usa `active/completed/escalated/failed` y el contrato de
 * la API `en_curso/completada/interrumpida`. Se aceptan los dos vocabularios
 * porque no está cerrado cuál publicará el backend, y una llamada escalada es,
 * de cara al historial, una llamada completada que además escaló.
 */
function normalizarEstado(valor: unknown): EstadoLlamada {
  const bruto = texto(valor)
  if ((ESTADOS_VALIDOS as string[]).includes(bruto)) return bruto as EstadoLlamada
  switch (bruto) {
    case 'active':
      return 'en_curso'
    case 'completed':
    case 'escalated':
      return 'completada'
    case 'failed':
      return 'interrumpida'
    default:
      return 'completada'
  }
}

export function normalizarCita(bruto: unknown): Cita {
  const c = objeto(bruto)
  return {
    filename: texto(c.filename ?? c.documento ?? c.source, 'documento'),
    heading: textoOpcional(c.heading),
    page: numeroOpcional(c.page),
  }
}

export function normalizarCitas(bruto: unknown): Cita[] {
  return lista(bruto)
    .map(normalizarCita)
    .filter((cita) => cita.filename !== '')
}

export function normalizarMs(bruto: unknown): EtapasLlamadaMs {
  const m = objeto(bruto)
  const salida: EtapasLlamadaMs = {}
  // `llm_ttft` es el nombre que usa el schema para el tiempo hasta el primer
  // token; para el panel es «Razonamiento» igual.
  const equivalencias: Array<[keyof EtapasLlamadaMs, string[]]> = [
    ['stt', ['stt']],
    ['retrieval', ['retrieval', 'rag']],
    ['llm', ['llm', 'llm_ttft']],
    ['tts', ['tts', 'tts_primera_frase']],
    ['total', ['total']],
  ]
  for (const [clave, alias] of equivalencias) {
    for (const nombre of alias) {
      const valor = numeroOpcional(m[nombre])
      if (valor !== null) {
        salida[clave] = valor
        break
      }
    }
  }
  return salida
}

function normalizarQuien(valor: unknown): QuienHabla {
  switch (texto(valor)) {
    case 'agente':
    case 'agent':
      return 'agente'
    case 'sistema':
    case 'system':
      return 'sistema'
    default:
      return 'paciente'
  }
}

export function normalizarTurno(bruto: unknown, indice: number): TurnoLlamada {
  const t = objeto(bruto)
  return {
    ordinal: numero(t.ordinal, indice + 1),
    quien: normalizarQuien(t.quien ?? t.role),
    texto: texto(t.texto ?? t.content),
    citas: normalizarCitas(t.citas ?? t.citations),
    ms: normalizarMs(t.ms ?? t.latencies),
  }
}

function camposComunes(bruto: Record<string, unknown>) {
  return {
    id: texto(bruto.id),
    paciente: texto(bruto.paciente, 'Paciente'),
    cirugia: textoOpcional(bruto.cirugia),
    iniciada: texto(bruto.iniciada ?? bruto.started_at),
    duracion_s: numeroOpcional(bruto.duracion_s),
    estado: normalizarEstado(bruto.estado ?? bruto.status),
    escalada: Boolean(bruto.escalada ?? bruto.escalated),
    motivo_escalada: textoOpcional(bruto.motivo_escalada ?? bruto.escalation_reason),
  }
}

export function normalizarResumenLlamada(bruto: unknown): ResumenLlamada {
  const ll = objeto(bruto)
  // `turnos` puede venir ya como lista si el backend reutiliza el serializador
  // del detalle. Contar es siempre correcto; asumir un número, no.
  const turnos = Array.isArray(ll.turnos) ? ll.turnos.length : numero(ll.turnos)
  return { ...camposComunes(ll), turnos }
}

export function normalizarListaLlamadas(bruto: unknown): ListaLlamadas {
  const datos = objeto(bruto)
  const llamadas = lista(datos.llamadas)
    .map(normalizarResumenLlamada)
    .filter((llamada) => llamada.id !== '')
  // Lo más reciente arriba: es lo que se acaba de grabar en la demo.
  llamadas.sort((a, b) => b.iniciada.localeCompare(a.iniciada))
  return { llamadas }
}

export function normalizarDetalleLlamada(bruto: unknown): LlamadaDetalle {
  const ll = objeto(bruto)
  const crudos = Array.isArray(ll.turnos) ? ll.turnos : lista(ll.turnos_detalle)
  const turnos = crudos.map(normalizarTurno)
  turnos.sort((a, b) => a.ordinal - b.ordinal)
  return { ...camposComunes(ll), turnos }
}
