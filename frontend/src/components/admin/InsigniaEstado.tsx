import {
  Archive,
  CheckCircle2,
  FileSearch,
  Inbox,
  Loader2,
  Scissors,
  TriangleAlert,
  Waypoints,
} from 'lucide-react'
import type { ComponentType } from 'react'

import { Insignia } from '@/components/ui/insignia'
import { cn } from '@/lib/utils'
import type { EstadoDocumento } from '@/types/api'
import { definicionEstado } from '@/types/estados'

const ICONOS: Record<EstadoDocumento, ComponentType<{ className?: string }>> = {
  uploaded: Inbox,
  parsing: FileSearch,
  chunking: Scissors,
  embedding: Waypoints,
  ready: CheckCircle2,
  failed: TriangleAlert,
  superseded: Archive,
}

/**
 * Etiqueta de estado. El texto y el color salen del contrato sin retoques: la
 * pantalla y el backend tienen que contar la misma historia con las mismas
 * palabras, sobre todo la de «Listo — el agente ya lo sabe», que es la frase que
 * el jurado se lleva a casa.
 */
export function InsigniaEstado({
  estado,
  className,
}: {
  estado: EstadoDocumento
  className?: string
}) {
  const definicion = definicionEstado(estado)
  const Icono = ICONOS[estado] ?? Inbox
  const trabajando = definicion.enCurso && estado !== 'uploaded'

  return (
    <Insignia
      color={definicion.color}
      className={cn(estado === 'ready' && 'font-semibold', className)}
      icono={
        trabajando ? (
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <Icono className="size-3.5" aria-hidden />
        )
      }
    >
      {definicion.etiqueta}
    </Insignia>
  )
}
