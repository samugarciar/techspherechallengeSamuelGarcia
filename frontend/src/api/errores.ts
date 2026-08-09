import type { CodigoError, CuerpoError } from '@/types/api'

/**
 * Error de la API con el código del contrato dentro.
 *
 * El contrato garantiza `{ error: { codigo, mensaje } }` y que el mensaje es
 * mostrable al usuario. Aun así la UI nunca lo pinta a pelo: si el backend está
 * caído no hay cuerpo que leer, y ahí es donde una pantalla se queda en blanco.
 * Por eso todo error acaba en esta clase, con un mensaje en español siempre.
 */
export class ErrorApi extends Error {
  readonly codigo: CodigoError
  readonly estadoHttp: number
  /** Detalle técnico para consola; nunca se pinta. */
  readonly causaTecnica?: string

  constructor(codigo: CodigoError, mensaje: string, estadoHttp = 0, causaTecnica?: string) {
    super(mensaje)
    this.name = 'ErrorApi'
    this.codigo = codigo
    this.estadoHttp = estadoHttp
    this.causaTecnica = causaTecnica
  }
}

/** Códigos propios del cliente, que el backend nunca emite. */
export const SIN_CONEXION = 'sin_conexion'
export const TIEMPO_AGOTADO = 'tiempo_agotado'
export const RESPUESTA_ILEGIBLE = 'respuesta_ilegible'

const MENSAJES_POR_ESTADO: Record<number, string> = {
  400: 'La petición no era válida.',
  401: 'Token de administrador incorrecto o ausente.',
  403: 'El token no tiene permiso para esta operación.',
  404: 'No se encontró lo que buscabas.',
  409: 'Conflicto con el estado actual del servidor.',
  413: 'El archivo supera el tamaño máximo admitido (25 MB).',
  415: 'Formato de archivo no admitido.',
  422: 'Faltan datos o no tienen el formato esperado.',
  500: 'Error interno del servidor.',
  502: 'El servidor no respondió correctamente.',
  503: 'El servicio no está disponible ahora mismo.',
}

export function errorDesdeRespuesta(estado: number, cuerpo: unknown): ErrorApi {
  const posible = cuerpo as Partial<CuerpoError> | null
  const detalle = posible?.error
  if (detalle && typeof detalle.mensaje === 'string' && detalle.mensaje.trim()) {
    return new ErrorApi(detalle.codigo ?? 'error_interno', detalle.mensaje, estado)
  }
  const generico = MENSAJES_POR_ESTADO[estado] ?? `El servidor respondió con un error ${estado}.`
  return new ErrorApi(estado === 401 ? 'no_autorizado' : 'error_interno', generico, estado)
}

export function errorDeRed(causa: unknown): ErrorApi {
  const tecnico = causa instanceof Error ? causa.message : String(causa)
  if (causa instanceof DOMException && causa.name === 'TimeoutError') {
    return new ErrorApi(
      TIEMPO_AGOTADO,
      'El servidor tardó demasiado en responder. Puede estar procesando o caído.',
      0,
      tecnico,
    )
  }
  return new ErrorApi(
    SIN_CONEXION,
    'No se puede contactar con el backend. Comprueba que está arrancado en el puerto 8000.',
    0,
    tecnico,
  )
}

/** Texto que se pinta. Nunca devuelve vacío. */
export function mensajeDeError(error: unknown): string {
  if (error instanceof ErrorApi) return error.message
  if (error instanceof Error && error.message) return error.message
  return 'Ocurrió un error inesperado.'
}
