/**
 * Coerción de escalares que llegan por la red.
 *
 * Los dos normalizadores —`api/normalizar.ts` para documentos y
 * `api/llamadas/normalizar.ts` para llamadas— se escribieron en paralelo por
 * agentes distintos y cada uno se declaró estas mismas cinco funciones, con el
 * mismo cuerpo y distinto nombre (`entero` frente a `numero`). Viven aquí porque
 * el problema es literalmente el mismo: un campo del contrato puede llegar con
 * otro tipo, o no llegar, y la regla del proyecto es que eso degrade la celda a
 * «—» y nunca tumbe la pantalla.
 *
 * Lo que NO va aquí es la forma de cada objeto del contrato: cada normalizador
 * conoce su parte de la API y esa parte no se comparte.
 */

export function texto(valor: unknown, porDefecto = ''): string {
  return typeof valor === 'string' ? valor : porDefecto
}

export function textoOpcional(valor: unknown): string | null {
  return typeof valor === 'string' && valor.trim() !== '' ? valor : null
}

export function numero(valor: unknown, porDefecto = 0): number {
  return typeof valor === 'number' && Number.isFinite(valor) ? valor : porDefecto
}

export function numeroOpcional(valor: unknown): number | null {
  return typeof valor === 'number' && Number.isFinite(valor) ? valor : null
}

export function objeto(valor: unknown): Record<string, unknown> {
  return valor !== null && typeof valor === 'object' ? (valor as Record<string, unknown>) : {}
}

export function lista(valor: unknown): unknown[] {
  return Array.isArray(valor) ? valor : []
}
