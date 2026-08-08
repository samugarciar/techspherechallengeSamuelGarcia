/** Formateo para lectura humana. Todo en español de España: coma decimal y 24 h. */

const FECHA_LARGA = new Intl.DateTimeFormat('es-ES', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

const FECHA_CORTA = new Intl.DateTimeFormat('es-ES', {
  day: '2-digit',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
})

const RELATIVA = new Intl.RelativeTimeFormat('es-ES', { numeric: 'auto' })

export function formatearTamano(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const unidades = ['kB', 'MB', 'GB']
  let valor = bytes / 1024
  let i = 0
  while (valor >= 1024 && i < unidades.length - 1) {
    valor /= 1024
    i += 1
  }
  const decimales = valor < 10 ? 1 : 0
  return `${valor.toFixed(decimales).replace('.', ',')} ${unidades[i]}`
}

function aFecha(iso: string): Date | null {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatearFecha(iso: string): string {
  const d = aFecha(iso)
  return d ? FECHA_LARGA.format(d) : '—'
}

export function formatearFechaCorta(iso: string): string {
  const d = aFecha(iso)
  return d ? FECHA_CORTA.format(d) : '—'
}

/** «hace 2 minutos». Para la columna de fecha, que en la demo siempre es reciente. */
export function formatearRelativo(iso: string): string {
  const d = aFecha(iso)
  if (!d) return '—'
  const segundos = Math.round((d.getTime() - Date.now()) / 1000)
  const absoluto = Math.abs(segundos)
  if (absoluto < 45) return 'hace unos segundos'
  if (absoluto < 3600) return RELATIVA.format(Math.round(segundos / 60), 'minute')
  if (absoluto < 86_400) return RELATIVA.format(Math.round(segundos / 3600), 'hour')
  if (absoluto < 2_592_000) return RELATIVA.format(Math.round(segundos / 86_400), 'day')
  return FECHA_CORTA.format(d)
}

export function formatearMs(ms: number | undefined): string {
  if (ms === undefined || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(2).replace('.', ',')} s`
}

/** Score del reranker, 0–1, con dos decimales y coma. */
export function formatearScore(score: number): string {
  if (!Number.isFinite(score)) return '—'
  return score.toFixed(2).replace('.', ',')
}

export function formatearNumero(n: number): string {
  return new Intl.NumberFormat('es-ES').format(n)
}

/** Plural sencillo: `pluralizar(1, 'fragmento')` -> «1 fragmento». */
export function pluralizar(n: number, singular: string, plural = `${singular}s`): string {
  return `${formatearNumero(n)} ${n === 1 ? singular : plural}`
}
