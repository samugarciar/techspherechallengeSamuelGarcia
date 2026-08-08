import { errorDeRed, errorDesdeRespuesta, ErrorApi, RESPUESTA_ILEGIBLE } from '@/api/errores'
import { obtenerToken } from '@/lib/token'

/**
 * Capa HTTP mínima.
 *
 * Vacío por defecto = mismo origen: en desarrollo el proxy de Vite reenvía `/api`
 * a localhost:8000 (ver `vite.config.ts`), y así el navegador nunca ve una
 * petición cruzada. Se puede forzar una base absoluta con `VITE_API_BASE` si el
 * backend acaba viviendo en otro sitio, pero entonces habrá que habilitar CORS.
 */
export const BASE_API = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

/** 15 s es de sobra para cualquier endpoint del contrato: la ingesta es asíncrona
 *  y `POST /api/documents` responde 202 sin esperar al procesamiento. */
const TIEMPO_LIMITE_MS = 15_000

type Parametros = Record<string, string | number | undefined | null>

export function urlApi(camino: string, parametros?: Parametros): string {
  const consulta = new URLSearchParams()
  for (const [clave, valor] of Object.entries(parametros ?? {})) {
    if (valor === undefined || valor === null || valor === '') continue
    consulta.set(clave, String(valor))
  }
  const cadena = consulta.toString()
  return `${BASE_API}${camino}${cadena ? `?${cadena}` : ''}`
}

function cabeceras(extra?: HeadersInit): Headers {
  const h = new Headers(extra)
  const token = obtenerToken()
  if (token) h.set('X-Admin-Token', token)
  return h
}

async function cuerpoJson(respuesta: Response): Promise<unknown> {
  const texto = await respuesta.text()
  if (!texto) return null
  try {
    return JSON.parse(texto) as unknown
  } catch {
    return null
  }
}

export async function pedirJson<T>(
  camino: string,
  opciones: RequestInit & { parametros?: Parametros } = {},
): Promise<T> {
  const { parametros, signal, ...resto } = opciones
  let respuesta: Response
  try {
    respuesta = await fetch(urlApi(camino, parametros), {
      ...resto,
      headers: cabeceras(resto.headers),
      signal: signal ?? AbortSignal.timeout(TIEMPO_LIMITE_MS),
    })
  } catch (causa) {
    if (causa instanceof DOMException && causa.name === 'AbortError' && signal?.aborted) {
      throw causa // cancelación deliberada de quien llama: no es un fallo de red
    }
    throw errorDeRed(causa)
  }

  const datos = await cuerpoJson(respuesta)
  if (!respuesta.ok) throw errorDesdeRespuesta(respuesta.status, datos)
  if (datos === null && respuesta.status !== 204) {
    throw new ErrorApi(RESPUESTA_ILEGIBLE, 'El servidor devolvió una respuesta vacía o ilegible.')
  }
  return datos as T
}

/**
 * Subida con XMLHttpRequest en vez de `fetch`.
 *
 * `fetch` no expone progreso de subida en ningún navegador de escritorio hoy.
 * Con archivos de hasta 25 MB eso es una barra que no se mueve durante segundos,
 * justo en la acción que el jurado está mirando.
 */
export function subirConProgreso<T>(
  camino: string,
  formulario: FormData,
  opciones: { onProgreso?: (fraccion: number) => void; signal?: AbortSignal } = {},
): Promise<T> {
  return new Promise<T>((resolver, rechazar) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', urlApi(camino))
    const token = obtenerToken()
    if (token) xhr.setRequestHeader('X-Admin-Token', token)
    xhr.responseType = 'text'
    xhr.timeout = 120_000 // 25 MB por una red lenta puede tardar

    xhr.upload.addEventListener('progress', (evento) => {
      if (!evento.lengthComputable) return
      opciones.onProgreso?.(evento.loaded / evento.total)
    })

    xhr.addEventListener('load', () => {
      let datos: unknown = null
      try {
        datos = xhr.responseText ? (JSON.parse(xhr.responseText) as unknown) : null
      } catch {
        datos = null
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        if (datos === null) {
          rechazar(new ErrorApi(RESPUESTA_ILEGIBLE, 'El servidor aceptó el archivo pero no devolvió el documento.'))
          return
        }
        opciones.onProgreso?.(1)
        resolver(datos as T)
      } else {
        rechazar(errorDesdeRespuesta(xhr.status, datos))
      }
    })

    xhr.addEventListener('error', () => rechazar(errorDeRed(new Error('fallo de red en XHR'))))
    xhr.addEventListener('timeout', () =>
      rechazar(new ErrorApi('tiempo_agotado', 'La subida tardó demasiado y se canceló.')),
    )
    xhr.addEventListener('abort', () =>
      rechazar(new DOMException('Subida cancelada', 'AbortError')),
    )

    opciones.signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(formulario)
  })
}
