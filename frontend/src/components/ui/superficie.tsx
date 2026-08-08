import type { ComponentProps, ReactNode } from 'react'

import { cn } from '@/lib/utils'

/** Contenedores neutros: tarjeta, cabecera de sección y separador. */

export function Tarjeta({ className, ...props }: ComponentProps<'section'>) {
  return (
    <section
      className={cn('rounded-consola border border-borde bg-superficie', className)}
      {...props}
    />
  )
}

export function CabeceraTarjeta({
  titulo,
  descripcion,
  acciones,
  className,
}: {
  titulo: ReactNode
  descripcion?: ReactNode
  acciones?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex flex-wrap items-start justify-between gap-3 border-b border-borde px-5 py-4',
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="text-[0.9375rem] font-semibold leading-tight text-tinta">{titulo}</h2>
        {descripcion ? (
          <p className="mt-1 text-[0.8125rem] leading-snug text-tinta-tenue">{descripcion}</p>
        ) : null}
      </div>
      {acciones ? <div className="flex shrink-0 items-center gap-2">{acciones}</div> : null}
    </div>
  )
}

export function Esqueleto({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded bg-superficie-tenue', className)} />
}
