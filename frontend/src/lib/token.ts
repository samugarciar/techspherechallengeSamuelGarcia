/**
 * Token de administrador.
 *
 * Un solo administrador, sin sesiones ni JWT: eso está decidido en el contrato.
 * Vive en localStorage porque la alternativa —pedirlo en cada recarga— convierte
 * la demo en un trámite. No es un secreto de producción; el día que haya varios
 * usuarios esto se sustituye entero, no se parchea.
 *
 * Se publica un `store` compatible con `useSyncExternalStore` para que la cabecera
 * y la capa de red vean siempre el mismo valor, incluso si se cambia en otra
 * pestaña (el evento `storage` del navegador ya lo propaga).
 */

const CLAVE = 'postop.admin.token'

const oyentes = new Set<() => void>()
let cacheado: string = leerDelAlmacen()

function leerDelAlmacen(): string {
  try {
    return window.localStorage.getItem(CLAVE) ?? ''
  } catch {
    // Modo privado de Safari o almacenamiento bloqueado: se sigue funcionando,
    // solo que el token no sobrevive a la recarga.
    return ''
  }
}

function avisar() {
  for (const oyente of oyentes) oyente()
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (evento) => {
    if (evento.key !== CLAVE) return
    cacheado = evento.newValue ?? ''
    avisar()
  })
}

export function obtenerToken(): string {
  return cacheado
}

export function guardarToken(valor: string): void {
  cacheado = valor.trim()
  try {
    if (cacheado) window.localStorage.setItem(CLAVE, cacheado)
    else window.localStorage.removeItem(CLAVE)
  } catch {
    /* sin persistencia; el valor en memoria sigue siendo válido */
  }
  avisar()
}

export const almacenToken = {
  suscribir(oyente: () => void) {
    oyentes.add(oyente)
    return () => oyentes.delete(oyente)
  },
  leer: obtenerToken,
}
