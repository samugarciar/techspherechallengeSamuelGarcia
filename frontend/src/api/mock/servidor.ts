import { CORPUS, terminos } from '@/api/mock/corpus'
import { ErrorApi } from '@/api/errores'
import { validarArchivo } from '@/lib/archivos'
import type {
  Documento,
  EstadoDocumento,
  FragmentoRag,
  ListaDocumentos,
  RespuestaRag,
  ResultadoBorrado,
  Salud,
  TrozoPrevio,
} from '@/types/api'

/**
 * ===========================================================================
 *  BACKEND SIMULADO — NO ENTRA EN PRODUCCIÓN
 * ===========================================================================
 *
 * Todo lo que hay bajo `src/api/mock/` existe por una razón de calendario: el
 * backend se escribe en paralelo y esta pantalla tenía que poder construirse y
 * enseñarse la misma noche. Implementa el contrato de `docs/CONTRATO_API.md` de
 * memoria, con la máquina de estados completa y retardos verosímiles.
 *
 * Se activa con `VITE_MOCK=1`. Apagarlo es borrar esa variable: ni un import de
 * la UI apunta aquí, todo pasa por `src/api/index.ts`.
 *
 * Lo que SÍ simula fielmente:
 *   · la secuencia uploaded → parsing → chunking → embedding → ready
 *   · `embedded_count` subiendo trozo a trozo, que es el corazón de la demo
 *   · el 202 inmediato de la subida (no espera al procesamiento)
 *   · el borrado en cascada, devolviendo cuántos trozos se llevó por delante
 *   · la regla de reemplazo por sha256: la versión vieja pasa a `superseded` y
 *     se borra, sin que lleguen a coexistir dos versiones consultables
 *   · un RAG léxico de verdad (no devuelve texto fijo): si borras el documento,
 *     la consulta deja de encontrar evidencia. Eso es lo que hay que enseñar.
 *
 * Lo que NO simula: embeddings reales, reranker, ni el orden exacto que dará
 * pgvector. Los `score` son de solape de términos, no de coseno.
 */

type Oyente = (evento: EventoSimulado) => void

export type EventoSimulado =
  | { tipo: 'documento'; documento: Documento }
  | { tipo: 'eliminado'; id: string }
  | { tipo: 'latido' }
  | { tipo: 'caida' }

interface Registro {
  documento: Documento
  trozos: TrozoPrevio[]
}

/** Retardos de la máquina de estados. Multiplicar `VELOCIDAD` acelera la demo. */
const VELOCIDAD = 1
const RETARDO = {
  subida: 500,
  parsing: 1_500,
  chunking: 1_100,
  porTrozo: 95,
  entreLotes: 220,
  borradoDeReemplazado: 2_500,
}

const LATIDO_MS = 15_000

function ms(valor: number) {
  return valor / VELOCIDAD
}

function ahora(): string {
  return new Date().toISOString()
}

function uuid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `sim-${Math.random().toString(36).slice(2, 10)}`
}

function mimeDe(nombre: string): string {
  if (nombre.endsWith('.pdf')) return 'application/pdf'
  if (nombre.endsWith('.docx'))
    return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  if (nombre.endsWith('.md')) return 'text/markdown'
  return 'text/plain'
}

/** Un sha falso pero estable: mismo nombre y tamaño -> mismo hash, que es justo
 *  la condición que dispara la regla de «nueva versión» del contrato. */
function shaFalso(nombre: string, tamano: number): string {
  let h = 2166136261
  for (const caracter of `${nombre}:${tamano}`) {
    h ^= caracter.charCodeAt(0)
    h = Math.imul(h, 16777619)
  }
  const base = (h >>> 0).toString(16).padStart(8, '0')
  return base.repeat(8)
}

function tituloDesdeNombre(nombre: string): string {
  const sinExtension = nombre.replace(/\.[^.]+$/, '')
  const limpio = sinExtension.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim()
  return limpio.charAt(0).toUpperCase() + limpio.slice(1)
}

function numeroDeTrozos(tamano: number): number {
  return Math.max(6, Math.min(32, Math.round(tamano / 1500)))
}

/**
 * Genera los trozos de un documento a partir de una selección de secciones.
 *
 * `temas` importa más de lo que parece. Si todos los documentos simulados
 * contuvieran las mismas secciones, borrar uno no cambiaría nunca la respuesta
 * del RAG —siempre quedaría otro con el mismo texto— y la demostración de
 * «olvidar» sería una mentira cómoda. Con temas disjuntos, cada documento es el
 * único que responde a lo suyo, y su borrado se nota de inmediato.
 */
function generarTrozos(titulo: string, cuantos: number, temas?: number[]): TrozoPrevio[] {
  const secciones = temas?.length
    ? temas.map((indice) => CORPUS[indice]).filter((s): s is (typeof CORPUS)[number] => Boolean(s))
    : CORPUS

  const trozos: TrozoPrevio[] = []
  for (let i = 0; i < cuantos; i += 1) {
    const seccion = secciones[i % secciones.length]
    if (!seccion) continue
    const vuelta = Math.floor(i / secciones.length)
    trozos.push({
      ordinal: i,
      heading: `${titulo} › ${seccion.heading}`,
      content: vuelta === 0 ? seccion.content : `${seccion.content} (continuación ${vuelta + 1})`,
      page: Math.floor(i / 4) + 1,
    })
  }
  return trozos
}

// ---------------------------------------------------------------------------
// Servidor
// ---------------------------------------------------------------------------

class ServidorSimulado {
  private registros = new Map<string, Registro>()
  private oyentes = new Set<Oyente>()
  private temporizadores = new Set<ReturnType<typeof setTimeout>>()
  private latido: ReturnType<typeof setInterval> | undefined

  constructor() {
    this.sembrar()
  }

  // -- flujo de eventos ------------------------------------------------------

  suscribir(oyente: Oyente): () => void {
    this.oyentes.add(oyente)
    if (this.latido === undefined) {
      this.latido = setInterval(() => this.emitir({ tipo: 'latido' }), LATIDO_MS)
    }
    return () => {
      this.oyentes.delete(oyente)
      if (this.oyentes.size === 0 && this.latido !== undefined) {
        clearInterval(this.latido)
        this.latido = undefined
      }
    }
  }

  private emitir(evento: EventoSimulado) {
    for (const oyente of [...this.oyentes]) oyente(evento)
  }

  /** Corta el flujo a propósito, para ver el backoff de reconexión en acción. */
  simularCaida() {
    this.emitir({ tipo: 'caida' })
  }

  private programar(fn: () => void, retardo: number) {
    const id = setTimeout(() => {
      this.temporizadores.delete(id)
      fn()
    }, ms(retardo))
    this.temporizadores.add(id)
  }

  // -- endpoints -------------------------------------------------------------

  async salud(): Promise<Salud> {
    await this.latencia(40)
    return { ok: true, db: true, version: '0.1.0-simulado' }
  }

  async listar(parametros: {
    estado?: EstadoDocumento
    q?: string
    limit?: number
    offset?: number
  }): Promise<ListaDocumentos> {
    await this.latencia(120)
    const consulta = parametros.q?.trim().toLowerCase()
    let documentos = [...this.registros.values()].map((registro) => ({ ...registro.documento }))
    if (parametros.estado) documentos = documentos.filter((d) => d.status === parametros.estado)
    if (consulta) {
      documentos = documentos.filter(
        (d) =>
          d.filename.toLowerCase().includes(consulta) ||
          (d.title ?? '').toLowerCase().includes(consulta),
      )
    }
    documentos.sort((a, b) => b.created_at.localeCompare(a.created_at))
    const total = documentos.length
    const desde = parametros.offset ?? 0
    const hasta = desde + (parametros.limit ?? 100)
    return { documentos: documentos.slice(desde, hasta), total }
  }

  async subir(archivo: File, titulo?: string): Promise<Documento> {
    await this.latencia(RETARDO.subida)

    // El backend valida de nuevo: se replica aquí para que los códigos de error
    // del contrato se puedan ver en pantalla sin tener el backend delante.
    const motivo = validarArchivo(archivo)
    if (motivo) {
      const codigo =
        archivo.size === 0
          ? 'archivo_vacio'
          : archivo.size > 25 * 1024 * 1024
            ? 'archivo_demasiado_grande'
            : 'formato_no_soportado'
      throw new ErrorApi(codigo, motivo, 400)
    }

    const titulacion = titulo?.trim() || tituloDesdeNombre(archivo.name)
    const documento: Documento = {
      id: uuid(),
      filename: archivo.name,
      title: titulacion,
      mime_type: archivo.type || mimeDe(archivo.name),
      size_bytes: archivo.size,
      sha256: shaFalso(archivo.name, archivo.size),
      status: 'uploaded',
      error: null,
      chunks_count: 0,
      embedded_count: 0,
      pages: null,
      supersedes_id: null,
      created_at: ahora(),
      updated_at: ahora(),
    }

    this.registros.set(documento.id, { documento, trozos: [] })
    this.emitir({ tipo: 'documento', documento: { ...documento } })
    this.arrancarIngesta(documento.id)
    return { ...documento }
  }

  async detalle(id: string): Promise<Documento & { chunks_preview: TrozoPrevio[] }> {
    await this.latencia(90)
    const registro = this.exigir(id)
    return {
      ...registro.documento,
      // El contrato promete los 3 primeros trozos, con 200 caracteres cada uno.
      chunks_preview: registro.trozos.slice(0, 3).map((trozo) => ({
        ...trozo,
        content: trozo.content.slice(0, 200),
      })),
    }
  }

  async eliminar(id: string): Promise<ResultadoBorrado> {
    await this.latencia(260)
    const registro = this.exigir(id)
    const eliminados = registro.documento.chunks_count
    this.registros.delete(id)
    this.emitir({ tipo: 'eliminado', id })
    return { olvidado: true, chunks_eliminados: eliminados }
  }

  /**
   * RAG léxico sobre los trozos de los documentos `ready`.
   *
   * Es deliberadamente simple —solape de términos, sin vectores— porque su único
   * trabajo es ser HONESTO respecto a qué documentos están vivos: si el trozo ya
   * no está, no aparece. Esa es la propiedad que la demo tiene que enseñar.
   */
  async consultarRag(consulta: string, topK: number): Promise<RespuestaRag> {
    const arranque = performance.now()
    await this.latencia(180)

    const buscados = terminos(consulta)
    const candidatos: FragmentoRag[] = []

    for (const registro of this.registros.values()) {
      // El filtro por `ready` no es una optimización: reproduce la vista
      // `retrievable_chunks` del schema, que es donde vive el «olvidar».
      if (registro.documento.status !== 'ready') continue
      for (const trozo of registro.trozos) {
        const enCuerpo = new Set(terminos(trozo.content))
        const enEncabezado = new Set(terminos(trozo.heading ?? ''))
        let puntos = 0
        for (const termino of buscados) {
          if (enEncabezado.has(termino)) puntos += 2
          else if (enCuerpo.has(termino)) puntos += 1
        }
        if (puntos === 0) continue
        // El divisor es generoso a propósito: con 1,0 casi cualquier coincidencia
        // suelta pasaría el umbral y `hay_evidencia` no significaría nada.
        const score = Math.min(0.99, puntos / Math.max(1, buscados.length * 1.3))
        candidatos.push({
          documento_id: registro.documento.id,
          filename: registro.documento.filename,
          heading: trozo.heading,
          page: trozo.page,
          contenido: trozo.content,
          score,
          cita: `${registro.documento.filename} › ${trozo.heading ?? 'sin sección'} › p. ${trozo.page ?? 1}`,
        })
      }
    }

    candidatos.sort((a, b) => b.score - a.score)
    const fragmentos = candidatos.slice(0, Math.max(1, topK))
    const mejor = fragmentos[0]?.score ?? 0

    const total = Math.round(performance.now() - arranque)
    return {
      fragmentos,
      hay_evidencia: mejor >= 0.3,
      ms: {
        embedding: 24,
        retrieval: 12 + Math.round(Math.random() * 20),
        rerank: 108 + Math.round(Math.random() * 14),
        total,
      },
    }
  }

  // -- ingesta ---------------------------------------------------------------

  private arrancarIngesta(id: string) {
    this.programar(() => this.aEstado(id, 'parsing'), RETARDO.parsing)

    this.programar(() => {
      const registro = this.registros.get(id)
      if (!registro) return

      // Gancho de demo: un archivo con «fallo» o «escaneado» en el nombre revienta
      // en el parseo. Sirve para enseñar el estado rojo sin tener que corromper
      // un PDF a mano delante del jurado.
      if (/fallo|corrupto|escaneado/i.test(registro.documento.filename)) {
        this.actualizar(id, {
          status: 'failed',
          error:
            'No se pudo extraer texto: el archivo parece un escaneado sin capa de texto y el OCR no está habilitado.',
        })
        return
      }

      const cuantos = numeroDeTrozos(registro.documento.size_bytes)
      registro.trozos = generarTrozos(registro.documento.title ?? registro.documento.filename, cuantos)
      this.actualizar(id, {
        status: 'chunking',
        chunks_count: cuantos,
        pages: registro.documento.mime_type === 'application/pdf' ? Math.ceil(cuantos / 4) : null,
      })
      this.programar(() => this.embeber(id), RETARDO.chunking)
    }, RETARDO.parsing + RETARDO.chunking)
  }

  /** Sube `embedded_count` por lotes, como haría un worker real con bge-m3. */
  private embeber(id: string) {
    const registro = this.registros.get(id)
    if (!registro) return
    this.actualizar(id, { status: 'embedding', embedded_count: 0 })

    const total = registro.documento.chunks_count
    const lote = total > 20 ? 3 : 2
    let hechos = 0

    const paso = () => {
      const vivo = this.registros.get(id)
      if (!vivo || vivo.documento.status !== 'embedding') return // lo borraron a mitad
      hechos = Math.min(total, hechos + lote)
      this.actualizar(id, { embedded_count: hechos })
      if (hechos < total) {
        this.programar(paso, RETARDO.porTrozo * lote + RETARDO.entreLotes)
      } else {
        // La promoción a `ready` es atómica en el backend real: el agente pasa de
        // no conocer el documento a conocerlo entero, sin estados intermedios.
        this.programar(() => this.promocionar(id), 420)
      }
    }

    this.programar(paso, RETARDO.entreLotes)
  }

  private promocionar(id: string) {
    const registro = this.registros.get(id)
    if (!registro) return

    // Regla de reemplazo del contrato: si ya había un `ready` con el mismo sha,
    // el viejo deja de ser recuperable AL INSTANTE y se borra después. Nunca hay
    // dos versiones consultables del mismo contenido.
    const anterior = [...this.registros.values()].find(
      (otro) =>
        otro.documento.id !== id &&
        otro.documento.sha256 === registro.documento.sha256 &&
        otro.documento.status === 'ready',
    )

    this.actualizar(id, {
      status: 'ready',
      embedded_count: registro.documento.chunks_count,
      supersedes_id: anterior?.documento.id ?? null,
    })

    if (anterior) {
      this.actualizar(anterior.documento.id, { status: 'superseded' })
      this.programar(() => {
        if (!this.registros.has(anterior.documento.id)) return
        this.registros.delete(anterior.documento.id)
        this.emitir({ tipo: 'eliminado', id: anterior.documento.id })
      }, RETARDO.borradoDeReemplazado)
    }
  }

  private aEstado(id: string, status: EstadoDocumento) {
    this.actualizar(id, { status })
  }

  private actualizar(id: string, cambios: Partial<Documento>) {
    const registro = this.registros.get(id)
    if (!registro) return
    registro.documento = { ...registro.documento, ...cambios, updated_at: ahora() }
    this.emitir({ tipo: 'documento', documento: { ...registro.documento } })
  }

  private exigir(id: string): Registro {
    const registro = this.registros.get(id)
    if (!registro) {
      throw new ErrorApi(
        'documento_no_encontrado',
        'Ese documento ya no existe. Puede que lo haya borrado otra pestaña.',
        404,
      )
    }
    return registro
  }

  private latencia(base: number): Promise<void> {
    return new Promise((resolver) => setTimeout(resolver, ms(base + Math.random() * base * 0.3)))
  }

  // -- semilla ---------------------------------------------------------------

  private sembrar() {
    const hace = (minutos: number) => new Date(Date.now() - minutos * 60_000).toISOString()

    // Los `temas` son índices de CORPUS y están repartidos sin solaparse: así cada
    // pregunta tiene un único documento que la responde. «Higiene y ducha» (3) y
    // «Cita de revisión» (6) quedan fuera a propósito, para que la consola arranque
    // con un caso de «sin evidencia» que enseñar antes de subir nada.
    const semillas: Array<{
      filename: string
      title: string
      status: EstadoDocumento
      chunks: number
      pages: number | null
      size: number
      minutos: number
      temas: number[]
      error?: string
    }> = [
      {
        filename: 'protocolo_alta_apendicectomia.pdf',
        title: 'Protocolo de alta — apendicectomía laparoscópica',
        status: 'ready',
        chunks: 24,
        pages: 6,
        size: 184_320,
        minutos: 190,
        temas: [1, 9, 7, 5],
      },
      {
        filename: 'cuidados_herida_quirurgica.md',
        title: 'Cuidados de la herida quirúrgica',
        status: 'ready',
        chunks: 11,
        pages: null,
        size: 14_204,
        minutos: 95,
        temas: [0, 8],
      },
      {
        filename: 'pauta_analgesia_postoperatoria.docx',
        title: 'Pauta de analgesia postoperatoria',
        status: 'ready',
        chunks: 17,
        pages: 4,
        size: 62_918,
        minutos: 41,
        temas: [2],
      },
      {
        filename: 'guia_alta_colecistectomia_v1.pdf',
        title: 'Guía de alta — colecistectomía (versión 1)',
        status: 'superseded',
        chunks: 19,
        pages: 5,
        size: 151_002,
        minutos: 1_460,
        temas: [4],
      },
      {
        filename: 'informe_escaneado_2019.pdf',
        title: 'Informe escaneado 2019',
        status: 'failed',
        chunks: 0,
        pages: null,
        size: 3_204_118,
        minutos: 22,
        temas: [],
        error:
          'No se pudo extraer texto: el archivo parece un escaneado sin capa de texto y el OCR no está habilitado.',
      },
    ]

    for (const semilla of semillas) {
      const id = uuid()
      const documento: Documento = {
        id,
        filename: semilla.filename,
        title: semilla.title,
        mime_type: mimeDe(semilla.filename),
        size_bytes: semilla.size,
        sha256: shaFalso(semilla.filename, semilla.size),
        status: semilla.status,
        error: semilla.error ?? null,
        chunks_count: semilla.chunks,
        embedded_count: semilla.status === 'failed' ? 0 : semilla.chunks,
        pages: semilla.pages,
        supersedes_id: null,
        created_at: hace(semilla.minutos),
        updated_at: hace(semilla.minutos - 1),
      }
      this.registros.set(id, {
        documento,
        trozos: generarTrozos(semilla.title, semilla.chunks, semilla.temas),
      })
    }
  }
}

export const servidorSimulado = new ServidorSimulado()
