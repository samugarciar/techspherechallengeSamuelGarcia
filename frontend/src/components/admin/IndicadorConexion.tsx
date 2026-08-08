import { Loader2, Wifi, WifiOff } from 'lucide-react'

import type { EstadoConexion } from '@/api/cliente'
import { cn } from '@/lib/utils'

/**
 * Estado del flujo SSE, siempre visible.
 *
 * Importa más de lo que parece: si la conexión se cae, la tabla deja de moverse y
 * sin este indicador parecería que la ingesta se ha atascado. Aquí se distingue
 * «el servidor no responde» de «no está pasando nada».
 */
export function IndicadorConexion({
  estado,
  intento,
}: {
  estado: EstadoConexion
  intento: number
}) {
  const { texto, clase, icono } = describir(estado, intento)
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium',
        clase,
      )}
      title="Estado del flujo de eventos en vivo (SSE)"
    >
      {icono}
      {texto}
    </span>
  )
}

function describir(estado: EstadoConexion, intento: number) {
  switch (estado) {
    case 'conectado':
      return {
        texto: 'En vivo',
        clase: 'border-verde/30 bg-verde-suave text-verde',
        icono: <Wifi className="size-3.5" aria-hidden />,
      }
    case 'conectando':
      return {
        texto: 'Conectando…',
        clase: 'border-borde bg-superficie-tenue text-tinta-tenue',
        icono: <Loader2 className="size-3.5 animate-spin" aria-hidden />,
      }
    case 'reconectando':
      return {
        texto: intento > 1 ? `Reconectando (intento ${intento})` : 'Reconectando…',
        clase: 'border-ambar/35 bg-ambar-suave text-ambar',
        icono: <Loader2 className="size-3.5 animate-spin" aria-hidden />,
      }
    case 'inactivo':
      return {
        texto: 'Sin conectar',
        clase: 'border-borde bg-superficie-tenue text-tinta-tenue',
        icono: <WifiOff className="size-3.5" aria-hidden />,
      }
    case 'cerrado':
    default:
      return {
        texto: 'Desconectado',
        clase: 'border-borde bg-superficie-tenue text-tinta-tenue',
        icono: <WifiOff className="size-3.5" aria-hidden />,
      }
  }
}
