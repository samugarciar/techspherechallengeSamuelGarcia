import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

export type TonoAviso = 'info' | 'exito' | 'atencion' | 'error'

const ESTILOS: Record<TonoAviso, { caja: string; icono: ReactNode }> = {
  info: {
    caja: 'border-borde bg-superficie-tenue text-tinta',
    icono: <Info className="size-4 shrink-0 text-tinta-tenue" />,
  },
  exito: {
    caja: 'border-verde/30 bg-verde-suave text-tinta',
    icono: <CheckCircle2 className="size-4 shrink-0 text-verde" />,
  },
  atencion: {
    caja: 'border-ambar/35 bg-ambar-suave text-tinta',
    icono: <AlertTriangle className="size-4 shrink-0 text-ambar" />,
  },
  error: {
    caja: 'border-rojo/35 bg-rojo-suave text-tinta',
    icono: <XCircle className="size-4 shrink-0 text-rojo" />,
  },
}

export function Aviso({
  tono = 'info',
  titulo,
  children,
  acciones,
  className,
}: {
  tono?: TonoAviso
  titulo?: ReactNode
  children?: ReactNode
  acciones?: ReactNode
  className?: string
}) {
  const estilo = ESTILOS[tono]
  return (
    <div
      role={tono === 'error' ? 'alert' : 'status'}
      className={cn('flex gap-3 rounded-consola border px-4 py-3', estilo.caja, className)}
    >
      <span className="mt-0.5">{estilo.icono}</span>
      <div className="min-w-0 flex-1">
        {titulo ? <p className="text-sm font-semibold leading-snug">{titulo}</p> : null}
        {children ? (
          <div className={cn('text-[0.8125rem] leading-relaxed text-tinta-tenue', titulo && 'mt-1')}>
            {children}
          </div>
        ) : null}
      </div>
      {acciones ? <div className="flex shrink-0 items-start gap-2">{acciones}</div> : null}
    </div>
  )
}
