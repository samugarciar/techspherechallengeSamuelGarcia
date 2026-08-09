import type {
  Documento,
  DocumentoConTrozos,
  EstadoDocumento,
  EventoDocumento,
  EventoEliminado,
  ListaDocumentos,
  RespuestaRag,
  ResultadoBorrado,
  Salud,
} from '@/types/api'

/**
 * La interfaz que la pantalla consume.
 *
 * Existe para que el backend simulado y el real sean intercambiables sin que
 * ningún componente se entere: `src/api/index.ts` elige uno u otro y nadie más
 * vuelve a preguntar. Mañana, apagar el mock es cambiar una variable de entorno.
 */

export interface ParametrosListado {
  estado?: EstadoDocumento
  q?: string
  limit?: number
  offset?: number
}

export interface OpcionesSubida {
  title?: string
  /** Fracción 0–1 de bytes enviados. Con XHR se conoce de verdad; con `fetch` no. */
  onProgreso?: (fraccion: number) => void
  signal?: AbortSignal
}

export type EstadoConexion = 'inactivo' | 'conectando' | 'conectado' | 'reconectando' | 'cerrado'

export interface ManejadoresFlujo {
  onDocumento: (evento: EventoDocumento) => void
  onEliminado: (evento: EventoEliminado) => void
  onEstado: (estado: EstadoConexion, intento: number) => void
  /**
   * Se dispara en cada (re)conexión. Quien escucha DEBE recargar la lista entera.
   *
   * (Corrección de este comentario, que decía que el contrato «no numera los
   * eventos ni admite Last-Event-ID»: sí lo hace — cada evento lleva `id` con la
   * marca del servidor y `GET /api/documents/stream` reanuda desde ella. Lo que
   * pasa es que este cliente cierra el `EventSource` y abre uno nuevo para poder
   * meter su propio backoff, y un `EventSource` recién creado no manda esa
   * cabecera. Se mantiene así a propósito: la reanudación exacta ahorraría unos
   * pocos eventos y la recarga completa es la única forma de recuperar también
   * lo que el flujo no publica —`filename`, `title`, `size_bytes`— si el que
   * cambió fue un documento que esta pestaña no conocía.)
   */
  onResincronizar?: () => void
}

export interface Suscripcion {
  cerrar: () => void
}

export interface ClienteApi {
  /** `true` si detrás no hay backend, sino el simulador de `src/api/mock/`. */
  readonly esSimulado: boolean
  salud: () => Promise<Salud>
  listar: (parametros?: ParametrosListado) => Promise<ListaDocumentos>
  subir: (archivo: File, opciones?: OpcionesSubida) => Promise<Documento>
  detalle: (id: string) => Promise<DocumentoConTrozos>
  eliminar: (id: string) => Promise<ResultadoBorrado>
  consultarRag: (consulta: string, topK?: number) => Promise<RespuestaRag>
  abrirFlujo: (manejadores: ManejadoresFlujo) => Suscripcion
}
