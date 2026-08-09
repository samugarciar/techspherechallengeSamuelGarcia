/**
 * Duraciones de llamada, en las tres formas que la pantalla necesita: reloj de la
 * transcripción, duración total del historial y días desde la cirugía.
 *
 * Aparte de `formato.ts` porque aquello formatea magnitudes sueltas —bytes,
 * fechas, milisegundos— y esto formatea el paso del tiempo *dentro de una
 * llamada*, que es donde se decide si se lee «2:07» o «3 min 4 s».
 */

/** Sello de tiempo dentro de la llamada: `2:07`. */
export function reloj(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const minutos = Math.floor(total / 60)
  const segundos = total % 60
  return `${minutos}:${String(segundos).padStart(2, '0')}`
}

/** Duración legible: «3 min 4 s». Para el historial, donde no hay reloj corriendo. */
export function formatearDuracion(segundos: number | null): string {
  if (segundos === null || !Number.isFinite(segundos)) return '—'
  if (segundos < 60) return `${Math.round(segundos)} s`
  const minutos = Math.floor(segundos / 60)
  const resto = Math.round(segundos % 60)
  return resto === 0 ? `${minutos} min` : `${minutos} min ${resto} s`
}

/** «3 días» / «ayer» / «hoy», para los días transcurridos desde la cirugía. */
export function diasDesde(dias: number | null): string {
  if (dias === null || !Number.isFinite(dias)) return 'sin fecha de cirugía'
  if (dias <= 0) return 'operado hoy'
  if (dias === 1) return 'operado ayer'
  return `${dias} días desde la cirugía`
}
