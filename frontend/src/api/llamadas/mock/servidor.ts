import type { ManejadoresVoz, SesionVoz } from '@/api/llamadas/cliente'
import { construirGuion, type ModoGuion, type PasoGuion } from '@/api/llamadas/mock/guion'
import { ErrorApi } from '@/api/errores'
import type {
  Cita,
  EtapasLlamadaMs,
  ListaLlamadas,
  ListaPacientes,
  LlamadaCreada,
  LlamadaDetalle,
  Paciente,
  QuienHabla,
  TurnoLlamada,
} from '@/types/llamadas'

/**
 * ===========================================================================
 *  BACKEND DE LLAMADAS SIMULADO — NO ENTRA EN PRODUCCIÓN
 * ===========================================================================
 *
 * Mismo motivo que `src/api/mock/`: el backend de llamadas se escribe en
 * paralelo y esta pantalla —la principal de la demo— tenía que poder
 * construirse y ensayarse entera sin él. Implementa `docs/CONTRATO_LLAMADAS.md`
 * de memoria: los cuatro endpoints y el protocolo del WebSocket, con tiempos
 * verosímiles.
 *
 * Lo que SÍ simula fielmente:
 *   · la secuencia de fases escuchando → pensando → hablando de cada turno;
 *   · las parciales del STT consolidando al cerrar el turno;
 *   · las citas llegando pegadas a la respuesta que fundamentan;
 *   · latencias por etapa con dispersión, no cifras redondas de folleto;
 *   · la bandera roja a mitad y el corte del guion que exige la decisión 2;
 *   · el audio del agente ADELANTADO respecto a la reproducción, que es la
 *     condición sin la cual el barge-in no tiene nada que vaciar.
 *
 * Lo que NO simula: voz. El audio que emite son muestras a cero — suficiente
 * para que el buffer del cliente se llene de verdad y el vaciado descarte
 * milisegundos reales, pero no se oye nada. La pantalla lo avisa.
 */

const SR_ENTRADA = 16_000
const SR_SALIDA = 24_000

/** Velocidad de habla en caracteres por segundo. Un castellano normal ronda 14. */
const CARACTERES_POR_SEGUNDO = 14
const DURACION_MINIMA_S = 1.4

/** Trozos de audio de 60 ms, como los que manda el bucle real. */
const MS_TROZO_AUDIO = 60

/**
 * Cuánto audio va el servidor por delante de la reproducción.
 *
 * En el bucle real se midieron 5,4 s. La cifra importa: es exactamente lo que un
 * barge-in tiene que descartar, así que si el simulador soltara la frase entera
 * de golpe, el contador de «voz descartada» enseñaría veinte segundos y sería
 * mentira. Se emite por delante, pero sólo esto por delante.
 */
const ADELANTO_AUDIO_S = 5.4

function uuid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `sim-${Math.random().toString(36).slice(2, 10)}`
}

function iso(desplazamientoDias = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + desplazamientoDias)
  return d.toISOString().slice(0, 10)
}

function alrededor(base: number, dispersion = 0.25): number {
  return Math.round(base * (1 + (Math.random() * 2 - 1) * dispersion))
}

function duracionDe(texto: string): number {
  return Math.max(DURACION_MINIMA_S, texto.length / CARACTERES_POR_SEGUNDO)
}

// ---------------------------------------------------------------------------
// Reloj cancelable
// ---------------------------------------------------------------------------

/**
 * Espera cancelable. El guion es una secuencia de esperas y cerrar la pantalla
 * a mitad no puede dejar temporizadores emitiendo mensajes contra un componente
 * ya desmontado.
 */
class Reloj {
  private id: ReturnType<typeof setTimeout> | undefined
  private soltar: (() => void) | undefined
  private _cancelado = false

  esperar(ms: number): Promise<void> {
    return new Promise((resolver) => {
      this.soltar = resolver
      this.id = setTimeout(() => {
        this.id = undefined
        this.soltar = undefined
        resolver()
      }, ms)
    })
  }

  /** Termina la espera en curso ahora mismo (un barge-in no espera al reloj). */
  adelantar(): void {
    if (this.id !== undefined) clearTimeout(this.id)
    this.id = undefined
    const soltar = this.soltar
    this.soltar = undefined
    soltar?.()
  }

  cancelar(): void {
    this._cancelado = true
    this.adelantar()
  }

  get cancelado(): boolean {
    return this._cancelado
  }
}

// ---------------------------------------------------------------------------
// Estado del servidor
// ---------------------------------------------------------------------------

interface RegistroLlamada {
  id: string
  paciente_id: string
  paciente: string
  cirugia: string | null
  iniciada: string
  terminada: string | null
  estado: 'en_curso' | 'completada' | 'interrumpida'
  escalada: boolean
  motivo_escalada: string | null
  turnos: TurnoLlamada[]
}

/** Guion que usará la próxima llamada simulada. Lo elige la pantalla en modo mock. */
let modoGuion: ModoGuion = 'con_bandera'

export function elegirGuionSimulado(modo: ModoGuion): void {
  modoGuion = modo
}

export function guionSimulado(): ModoGuion {
  return modoGuion
}

/**
 * Velocidad del ensayo.
 *
 * Una llamada de seguimiento dura unos tres minutos de verdad, y ese es el
 * tiempo que tarda el guion a 1× — que es lo correcto para ensayar la demo tal
 * como se verá. Pero repasar la pantalla veinte veces mientras se construye a
 * ese ritmo es media tarde, así que se puede acelerar. Afecta a los tiempos y a
 * la cantidad de audio emitido por igual: acelerar no debe inflar la cola.
 */
let velocidad = 1

export const VELOCIDADES = [1, 2, 4, 8] as const

export function elegirVelocidadSimulada(nueva: number): void {
  velocidad = nueva > 0 ? nueva : 1
}

export function velocidadSimulada(): number {
  return velocidad
}

class ServidorLlamadasSimulado {
  private pacientesPorId = new Map<string, Paciente>()
  private llamadas = new Map<string, RegistroLlamada>()
  /** Sesión de voz viva, para poder interrumpirla o tirarla desde la UI. */
  private sesion: SesionSimulada | null = null

  constructor() {
    this.sembrarPacientes()
    this.sembrarHistorial()
  }

  // -- endpoints -------------------------------------------------------------

  async pacientes(): Promise<ListaPacientes> {
    await latencia(150)
    return { pacientes: [...this.pacientesPorId.values()].map((p) => ({ ...p })) }
  }

  async crearLlamada(pacienteId: string): Promise<LlamadaCreada> {
    await latencia(220)
    const paciente = this.pacientesPorId.get(pacienteId)
    if (!paciente) {
      throw new ErrorApi(
        'paciente_no_encontrado',
        'Ese paciente ya no está en la lista de seguimiento pendiente.',
        404,
      )
    }
    const id = uuid()
    this.llamadas.set(id, {
      id,
      paciente_id: paciente.id,
      paciente: paciente.nombre,
      cirugia: paciente.cirugia?.nombre ?? null,
      iniciada: new Date().toISOString(),
      terminada: null,
      estado: 'en_curso',
      escalada: false,
      motivo_escalada: null,
      turnos: [],
    })
    return { call_id: id, ws: `/ws/voz?call_id=${id}` }
  }

  async historial(): Promise<ListaLlamadas> {
    await latencia(160)
    const llamadas = [...this.llamadas.values()]
      .map((registro) => ({
        id: registro.id,
        paciente: registro.paciente,
        cirugia: registro.cirugia,
        iniciada: registro.iniciada,
        duracion_s: duracionSegundos(registro),
        estado: registro.estado,
        escalada: registro.escalada,
        motivo_escalada: registro.motivo_escalada,
        turnos: registro.turnos.length,
      }))
      .sort((a, b) => b.iniciada.localeCompare(a.iniciada))
    return { llamadas }
  }

  async detalleLlamada(id: string): Promise<LlamadaDetalle> {
    await latencia(140)
    const registro = this.llamadas.get(id)
    if (!registro) {
      throw new ErrorApi('llamada_no_encontrada', 'No existe ninguna llamada con ese id.', 404)
    }
    return {
      id: registro.id,
      paciente: registro.paciente,
      cirugia: registro.cirugia,
      iniciada: registro.iniciada,
      duracion_s: duracionSegundos(registro),
      estado: registro.estado,
      escalada: registro.escalada,
      motivo_escalada: registro.motivo_escalada,
      turnos: registro.turnos.map((turno) => ({ ...turno })),
    }
  }

  // -- voz -------------------------------------------------------------------

  abrirVoz(llamada: LlamadaCreada, manejadores: ManejadoresVoz): SesionVoz {
    const registro = this.llamadas.get(llamada.call_id)
    const paciente = registro ? this.pacientesPorId.get(registro.paciente_id) : undefined
    this.sesion?.cerrar()
    const sesion = new SesionSimulada(
      manejadores,
      registro ?? null,
      paciente ?? null,
      modoGuion,
      () => {
        if (this.sesion === sesion) this.sesion = null
      },
    )
    this.sesion = sesion
    return sesion
  }

  /** Corta la conexión simulada, para ver el estado de error sin apagar nada. */
  simularCaidaDeVoz(): void {
    this.sesion?.simularCaida()
  }

  /** Barge-in a mano: en el simulador no hay micrófono que interrumpa al agente. */
  interrumpirAgente(): void {
    this.sesion?.interrumpir()
  }

  get hayAgenteHablando(): boolean {
    return this.sesion?.hablando ?? false
  }

  // -- semillas --------------------------------------------------------------

  private sembrarPacientes() {
    // Los mismos tres de `backend/app/db/seed.sql`, con las fechas relativas a
    // hoy igual que allí. Así lo que se ensaya con el simulador se reconoce
    // luego contra el backend de verdad.
    const semillas: Paciente[] = [
      {
        id: 'aaaaaaaa-0000-0000-0000-000000000001',
        nombre: 'María Elena Restrepo Gómez',
        preferred_name: 'María',
        documento_cc: '1012345678',
        cirugia: {
          nombre: 'Apendicectomía laparoscópica',
          fecha: iso(-3),
          dias_desde: 3,
        },
        medicacion_activa: 2,
        proxima_cita: `${iso(7)}T10:00:00Z`,
        ultima_llamada: null,
      },
      {
        id: 'aaaaaaaa-0000-0000-0000-000000000002',
        nombre: 'Jorge Andrés Villalba Ruiz',
        preferred_name: 'Jorge',
        documento_cc: '1023456789',
        cirugia: {
          nombre: 'Colecistectomía laparoscópica',
          fecha: iso(-7),
          dias_desde: 7,
        },
        medicacion_activa: 1,
        proxima_cita: `${iso(3)}T15:30:00Z`,
        ultima_llamada: new Date(Date.now() - 4 * 86_400_000).toISOString(),
      },
      {
        id: 'aaaaaaaa-0000-0000-0000-000000000003',
        nombre: 'Lucía Fernanda Ospina Marín',
        preferred_name: 'Lucía',
        documento_cc: '1034567890',
        cirugia: {
          nombre: 'Herniorrafia inguinal derecha',
          fecha: iso(-1),
          dias_desde: 1,
        },
        medicacion_activa: 1,
        proxima_cita: `${iso(9)}T09:00:00Z`,
        ultima_llamada: null,
      },
    ]
    for (const paciente of semillas) this.pacientesPorId.set(paciente.id, paciente)
  }

  private sembrarHistorial() {
    const hace = (horas: number) => new Date(Date.now() - horas * 3_600_000).toISOString()

    const jorge: RegistroLlamada = {
      id: 'cccccccc-0000-0000-0000-000000000001',
      paciente_id: 'aaaaaaaa-0000-0000-0000-000000000002',
      paciente: 'Jorge Andrés Villalba Ruiz',
      cirugia: 'Colecistectomía laparoscópica',
      iniciada: hace(96),
      terminada: hace(96 - 0.052),
      estado: 'completada',
      escalada: false,
      motivo_escalada: null,
      turnos: turnosDe([
        ['agente', 'Buenos días. Le llamo del servicio de cirugía para el seguimiento de su operación. Soy un sistema automatizado, no una persona. ¿Hablo con Jorge Andrés Villalba?', [], { llm: 410, tts: 232 }],
        ['paciente', 'Sí, dígame.', [], { stt: 318 }],
        ['agente', 'Para confirmar su identidad, ¿me dice su número de cédula?', [], { llm: 288, tts: 190 }],
        ['paciente', 'Uno cero dos tres cuatro cinco seis siete ocho nueve.', [], { stt: 402 }],
        [
          'agente',
          'Correcto. Hace tres días le hicieron una colecistectomía. ¿Cómo tiene el dolor del cero al diez?',
          [{ filename: 'guia_alta_colecistectomia_v1.pdf', heading: 'Seguimiento a las 72 horas', page: 2 }],
          { retrieval: 88, llm: 512, tts: 244 },
        ],
        ['paciente', 'Un tres, se lleva bien.', [], { stt: 291 }],
        [
          'agente',
          '¿Y cómo ha tolerado las comidas con grasa? Es lo que más cuesta después de esta cirugía.',
          [{ filename: 'guia_alta_colecistectomia_v1.pdf', heading: 'Reintroducción de la dieta', page: 3 }],
          { retrieval: 104, llm: 588, tts: 210 },
        ],
        ['paciente', 'Regular, el primer día me sentó mal el desayuno, pero ya voy comiendo normal.', [], { stt: 455 }],
        [
          'agente',
          'Es lo habitual y no es motivo de alarma. Siga con comidas ligeras unos días más. Queda registrado que la recuperación va bien.',
          [{ filename: 'guia_alta_colecistectomia_v1.pdf', heading: 'Reintroducción de la dieta', page: 3 }],
          { retrieval: 76, llm: 604, tts: 268 },
        ],
      ]),
    }

    const lucia: RegistroLlamada = {
      id: 'cccccccc-0000-0000-0000-000000000002',
      paciente_id: 'aaaaaaaa-0000-0000-0000-000000000003',
      paciente: 'Lucía Fernanda Ospina Marín',
      cirugia: 'Herniorrafia inguinal derecha',
      iniciada: hace(26),
      terminada: hace(26 - 0.041),
      estado: 'completada',
      escalada: true,
      motivo_escalada: 'Sangrado activo por la herida a las 24 horas',
      turnos: turnosDe([
        ['agente', 'Buenas tardes. Le llamo del servicio de cirugía para el seguimiento de su operación. Soy un sistema automatizado. ¿Hablo con Lucía Fernanda Ospina?', [], { llm: 398, tts: 226 }],
        ['paciente', 'Sí, soy yo.', [], { stt: 264 }],
        ['agente', 'Para confirmar, ¿su número de cédula?', [], { llm: 244, tts: 172 }],
        ['paciente', 'Uno cero tres cuatro cinco seis siete ocho nueve cero.', [], { stt: 341 }],
        [
          'agente',
          'Correcto. Ayer le pusieron la malla inguinal. ¿Cómo está la herida? ¿Le sale algún líquido o sangre?',
          [{ filename: 'cuidados_herida_quirurgica.md', heading: 'Aspecto normal de la herida', page: null }],
          { retrieval: 92, llm: 471, tts: 238 },
        ],
        [
          'paciente',
          'Pues sí, me he levantado y el apósito estaba empapado de sangre. Me lo he cambiado y ha vuelto a mancharse.',
          [],
          { stt: 612 },
        ],
        [
          'sistema',
          'Bandera roja: sangrado activo por la herida a las 24 horas. Urgencia: urgente. El agente abandona el resto del cuestionario.',
          [],
          {},
        ],
        [
          'agente',
          'Lucía, escúcheme. Un apósito que se empapa y vuelve a mancharse no es lo esperable: hay que verla hoy. Haga presión firme sobre la herida con una gasa limpia y acuda ahora a urgencias del hospital donde la operaron. No conduzca usted. ¿Qué va a hacer ahora?',
          [
            { filename: 'cuidados_herida_quirurgica.md', heading: 'Sangrado por la herida', page: null },
            { filename: 'protocolo_alta_apendicectomia.pdf', heading: 'Cuándo acudir a urgencias', page: 5 },
          ],
          { retrieval: 118, llm: 703, tts: 254 },
        ],
        ['paciente', 'Hacer presión e irme a urgencias. Que me lleve mi hermano.', [], { stt: 498 }],
        [
          'agente',
          'Exacto. Aviso ahora mismo a su equipo médico de esta llamada. Vaya sin esperar.',
          [],
          { llm: 356, tts: 198 },
        ],
      ]),
    }

    this.llamadas.set(jorge.id, jorge)
    this.llamadas.set(lucia.id, lucia)
  }

}

function duracionSegundos(registro: RegistroLlamada): number | null {
  if (!registro.terminada) return null
  const inicio = new Date(registro.iniciada).getTime()
  const fin = new Date(registro.terminada).getTime()
  if (!Number.isFinite(inicio) || !Number.isFinite(fin)) return null
  return Math.max(0, Math.round((fin - inicio) / 1000))
}

type FilaTurno = [QuienHabla, string, Cita[], EtapasLlamadaMs]

function turnosDe(filas: FilaTurno[]): TurnoLlamada[] {
  return filas.map(([quien, texto, citas, ms], indice) => ({
    ordinal: indice + 1,
    quien,
    texto,
    citas,
    ms,
  }))
}

function latencia(base: number): Promise<void> {
  return new Promise((resolver) => setTimeout(resolver, base + Math.random() * base * 0.3))
}

// ---------------------------------------------------------------------------
// Sesión de voz simulada
// ---------------------------------------------------------------------------

class SesionSimulada implements SesionVoz {
  private reloj = new Reloj()
  private cerrada = false
  private interrumpido = false
  private _hablando = false

  // Campos declarados y asignados a mano en vez de propiedades de parámetro
  // (`constructor(private readonly x: T)`). El proyecto compila con
  // `erasableSyntaxOnly`, que prohíbe la sintaxis de TypeScript con efecto en
  // tiempo de ejecución para que el código sea borrable a JavaScript plano.
  private readonly manejadores: ManejadoresVoz
  private readonly registro: RegistroLlamada | null
  private readonly paciente: Paciente | null
  private readonly modo: ModoGuion
  private readonly alTerminar: () => void

  constructor(
    manejadores: ManejadoresVoz,
    registro: RegistroLlamada | null,
    paciente: Paciente | null,
    modo: ModoGuion,
    alTerminar: () => void,
  ) {
    this.manejadores = manejadores
    this.registro = registro
    this.paciente = paciente
    this.modo = modo
    this.alTerminar = alTerminar
    void this.ejecutar()
  }

  get hablando(): boolean {
    return this._hablando
  }

  // El simulador no escucha nada; el micrófono no se abre en modo simulado.
  enviarAudio(): void {}

  abierta(): boolean {
    return !this.cerrada
  }

  cerrar(): void {
    if (this.cerrada) return
    this.cerrada = true
    this.reloj.cancelar()
    this.manejadores.onEstado('cerrado')
    this.alTerminar()
  }

  simularCaida(): void {
    if (this.cerrada) return
    this.cerrada = true
    this.reloj.cancelar()
    if (this.registro && this.registro.estado === 'en_curso') {
      this.registro.estado = 'interrumpida'
      this.registro.terminada = new Date().toISOString()
    }
    this.manejadores.onEstado('cerrado')
    this.manejadores.onFallo('Se perdió la conexión de voz con el servidor.')
    this.alTerminar()
  }

  interrumpir(): void {
    if (this.cerrada || !this._hablando) return
    this.interrumpido = true
    // Exactamente lo que hace el servidor real: primero callar al cliente, que
    // tiene segundos de audio por delante, y después replantear el turno.
    this.emitir({ tipo: 'parar' })
    this.reloj.adelantar()
  }

  // -- ejecución del guion ---------------------------------------------------

  private emitir(mensaje: Parameters<ManejadoresVoz['onMensaje']>[0]): void {
    if (this.cerrada) return
    this.manejadores.onMensaje(mensaje)
  }

  /** Toda espera del guion pasa por aquí, que es donde se aplica la velocidad. */
  private esperar(ms: number): Promise<void> {
    return this.reloj.esperar(ms / velocidadSimulada())
  }

  private async ejecutar(): Promise<void> {
    this.manejadores.onEstado('conectando')
    await this.esperar(320)
    if (this.cerrada) return
    this.manejadores.onEstado('conectado')
    this.emitir({
      tipo: 'listo',
      sample_rate_entrada: SR_ENTRADA,
      sample_rate_salida: SR_SALIDA,
    })

    if (!this.paciente) {
      this.manejadores.onFallo('El simulador no encontró la ficha del paciente de esta llamada.')
      return
    }

    const guion = construirGuion(this.paciente, this.modo)
    await this.esperar(700)

    for (const paso of guion) {
      if (this.cerrada || this.reloj.cancelado) return
      await this.ejecutarPaso(paso)
    }
  }

  private async ejecutarPaso(paso: PasoGuion): Promise<void> {
    switch (paso.clase) {
      case 'agente':
        return this.turnoAgente(paso)
      case 'paciente':
        return this.turnoPaciente(paso)
      case 'bandera':
        return this.banderaRoja(paso)
      case 'fin':
        return this.finalizar(paso.motivo)
    }
  }

  private async turnoAgente(paso: Extract<PasoGuion, { clase: 'agente' }>): Promise<void> {
    this.emitir({ tipo: 'estado', fase: 'pensando' })

    // El «pensando» dura lo que tardan búsqueda y modelo; la voz empieza a sonar
    // en cuanto está la primera frase, no cuando está la respuesta entera.
    const ms: EtapasLlamadaMs = {
      retrieval: paso.citas?.length ? alrededor(95) : undefined,
      llm: alrededor(560, 0.35),
      tts: alrededor(230),
    }
    await this.esperar((ms.retrieval ?? 0) + (ms.llm ?? 0))
    if (this.cerrada) return

    if (paso.citas?.length) this.emitir({ tipo: 'citas', citas: paso.citas })
    await this.esperar(ms.tts ?? 0)
    if (this.cerrada) return

    this.emitir({ tipo: 'estado', fase: 'hablando' })
    this._hablando = true
    this.interrumpido = false

    const duracion = paso.duracion ?? duracionDe(paso.texto)

    // El audio se manda por delante de la reproducción, igual que el bucle real:
    // primero el adelanto de golpe, y luego reponiendo lo que se va consumiendo.
    // Sin ese adelanto no habría nada que vaciar en un barge-in y la
    // interrupción sería una animación bonita; con la frase entera de golpe, el
    // contador de voz descartada mentiría en sentido contrario.
    let audioEmitido = Math.min(duracion, ADELANTO_AUDIO_S)
    this.emitirAudio(audioEmitido)

    const dicho = await this.decirProgresivo(paso.texto, duracion, 'agente', (dichos) => {
      const objetivo = Math.min(duracion, dichos + ADELANTO_AUDIO_S)
      if (objetivo > audioEmitido) {
        this.emitirAudio(objetivo - audioEmitido)
        audioEmitido = objetivo
      }
    })
    this._hablando = false

    const textoFinal = this.interrumpido ? `${dicho.trim()}…` : paso.texto
    this.interrumpido = false // el turno ya se cerró; el siguiente empieza limpio
    this.emitir({ tipo: 'transcripcion', quien: 'agente', texto: textoFinal, parcial: false })
    this.emitir({ tipo: 'metricas', ms: { ...ms, total: (ms.retrieval ?? 0) + (ms.llm ?? 0) + (ms.tts ?? 0) } })
    this.anotar('agente', textoFinal, paso.citas ?? [], ms)

    if (this.cerrada) return
    this.emitir({ tipo: 'estado', fase: 'escuchando' })
  }

  private async turnoPaciente(paso: Extract<PasoGuion, { clase: 'paciente' }>): Promise<void> {
    this.emitir({ tipo: 'estado', fase: 'escuchando' })
    await this.esperar((paso.espera ?? 1.2) * 1000)
    if (this.cerrada) return

    await this.decirProgresivo(paso.texto, duracionDe(paso.texto), 'paciente')
    if (this.cerrada) return

    const ms: EtapasLlamadaMs = { stt: alrededor(360, 0.3) }
    this.emitir({ tipo: 'transcripcion', quien: 'paciente', texto: paso.texto, parcial: false })
    this.emitir({ tipo: 'metricas', ms })
    this.anotar('paciente', paso.texto, [], ms)
  }

  private async banderaRoja(paso: Extract<PasoGuion, { clase: 'bandera' }>): Promise<void> {
    this.emitir({ tipo: 'bandera_roja', motivo: paso.motivo, urgencia: paso.urgencia })
    if (this.registro) {
      this.registro.escalada = true
      this.registro.motivo_escalada = paso.motivo
      this.anotar(
        'sistema',
        `Bandera roja: ${paso.motivo}. Urgencia: ${paso.urgencia}. El agente abandona el resto del cuestionario.`,
        [],
        {},
      )
    }
    // Un respiro antes de que el agente reaccione: instantáneo se lee como un
    // fallo, y en la demo este es el momento que hay que dejar ver.
    await this.esperar(700)
  }

  private async finalizar(motivo: 'completada' | 'escalada' | 'cortada'): Promise<void> {
    if (this.registro) {
      this.registro.estado = motivo === 'cortada' ? 'interrumpida' : 'completada'
      this.registro.terminada = new Date().toISOString()
    }
    this.emitir({ tipo: 'fin', motivo })
    await this.esperar(200)
    this.cerrar()
  }

  /**
   * Va soltando parciales mientras «se habla», y devuelve lo que llegó a decirse.
   * Es lo que hace que la transcripción se vea crecer en vez de aparecer de golpe.
   */
  private async decirProgresivo(
    texto: string,
    duracion: number,
    quien: QuienHabla,
    alAvanzar?: (segundosDichos: number) => void,
  ): Promise<string> {
    const palabras = texto.split(' ')
    const pasos = Math.max(1, Math.ceil(palabras.length / 3))
    const porPaso = duracion / pasos
    let dicho = ''

    for (let i = 0; i < pasos; i += 1) {
      dicho = palabras.slice(0, Math.min(palabras.length, (i + 1) * 3)).join(' ')
      this.emitir({ tipo: 'transcripcion', quien, texto: dicho, parcial: true })
      alAvanzar?.(porPaso * (i + 1))
      await this.esperar(porPaso * 1000)
      if (this.cerrada || this.interrumpido) break
    }
    return dicho
  }

  /**
   * Muestras a cero: llenan el buffer del cliente sin sonar.
   *
   * La cantidad se divide por la velocidad de ensayo. Si no se dividiera, a 4×
   * el guion soltaría cuatro veces más audio del que da tiempo a reproducir y la
   * cola crecería sin parar hasta parecer un fallo del reproductor.
   */
  private emitirAudio(duracionS: number): void {
    if (this.cerrada) return
    const segundos = duracionS / velocidadSimulada()
    const porTrozo = Math.round((SR_SALIDA * MS_TROZO_AUDIO) / 1000)
    const trozos = Math.ceil((segundos * SR_SALIDA) / porTrozo)
    for (let i = 0; i < trozos; i += 1) {
      this.manejadores.onAudio(new Int16Array(porTrozo))
    }
  }

  /** Deja el turno grabado en la llamada, que es lo que luego lee el historial. */
  private anotar(quien: QuienHabla, texto: string, citas: Cita[], ms: EtapasLlamadaMs): void {
    if (!this.registro) return
    this.registro.turnos.push({
      ordinal: this.registro.turnos.length + 1,
      quien,
      texto,
      citas,
      ms,
    })
  }
}

export const servidorLlamadasSimulado = new ServidorLlamadasSimulado()
