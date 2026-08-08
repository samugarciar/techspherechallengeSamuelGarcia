import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'
import type { ColorEstado } from '@/types/estados'

/**
 * Insignia de color. Los cuatro colores son los del contrato y ninguno más:
 * si mañana aparece un estado nuevo, se añade allí primero.
 */
const PALETA: Record<ColorEstado, string> = {
  gris: 'bg-gris-suave text-gris border-gris/25',
  ambar: 'bg-ambar-suave text-ambar border-ambar/30',
  verde: 'bg-verde-suave text-verde border-verde/30',
  rojo: 'bg-rojo-suave text-rojo border-rojo/30',
}

export function Insignia({
  color,
  children,
  className,
  icono,
}: {
  color: ColorEstado
  children: ReactNode
  className?: string
  icono?: ReactNode
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium',
        PALETA[color],
        className,
      )}
    >
      {icono}
      {children}
    </span>
  )
}
