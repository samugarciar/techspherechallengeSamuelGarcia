import type { EstadoConexion } from '@/api/cliente'
import type {
  ListaLlamadas,
  ListaPacientes,
  LlamadaCreada,
  LlamadaDetalle,
  MensajeVoz,
} from '@/types/llamadas'

/**
 * La interfaz de llamadas que consume la pantalla.
 *
 * Existe por el mismo motivo que `ClienteApi` en `api/cliente.ts`: el backend de
 * llamadas se escribe en paralelo, así que la pantalla habla con una interfaz y
 * `api/llamadas/index.ts` decide si detrás hay FastAPI o el simulador. Ningún
 * componente importa nunca del simulador directamente.
 */

export interface ManejadoresVoz {
  /** Mensaje de control JSON, ya parseado. */
  onMensaje: (mensaje: MensajeVoz) => void
  /** Trozo de audio del agente: PCM int16 LE mono al `sample_rate_salida`. */
  onAudio: (pcm: Int16Array) => void
  onEstado: (estado: EstadoConexion) => void
  /**
   * La conexión se fue al garete y no va a volver sola. El texto ya viene en
   * español y mostrable: un WebSocket no expone el motivo del rechazo al
   * JavaScript de la página, así que el mensaje se construye aquí.
   */
  onFallo: (mensaje: string) => void
}

export interface SesionVoz {
  /** PCM int16 LE mono a 16 kHz, que es lo que esperan Silero y Whisper. */
  enviarAudio: (pcm: Int16Array) => void
  /** ¿Hay canal abierto ahora mismo? */
  abierta: () => boolean
  cerrar: () => void
}

export interface ClienteLlamadas {
  /** `true` si detrás no hay backend, sino el simulador de `api/llamadas/mock/`. */
  readonly esSimulado: boolean
  pacientes: () => Promise<ListaPacientes>
  crearLlamada: (pacienteId: string) => Promise<LlamadaCreada>
  historial: () => Promise<ListaLlamadas>
  detalleLlamada: (id: string) => Promise<LlamadaDetalle>
  abrirVoz: (llamada: LlamadaCreada, manejadores: ManejadoresVoz) => SesionVoz
}
