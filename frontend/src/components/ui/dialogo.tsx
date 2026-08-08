import * as Primitiva from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'

import { cn } from '@/lib/utils'

/** Diálogo modal sobre Radix: foco atrapado, Escape y aria ya resueltos. */

export const Dialogo = Primitiva.Root
export const DisparadorDialogo = Primitiva.Trigger

export function ContenidoDialogo({
  children,
  className,
  ...props
}: ComponentProps<typeof Primitiva.Content>) {
  return (
    <Primitiva.Portal>
      <Primitiva.Overlay className="fixed inset-0 z-50 bg-black/45 backdrop-blur-[1px]" />
      <Primitiva.Content
        className={cn(
          'fixed left-1/2 top-1/2 z-50 w-[min(40rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2',
          'max-h-[calc(100vh-3rem)] overflow-y-auto rounded-consola border border-borde bg-superficie shadow-xl',
          'focus:outline-none',
          className,
        )}
        {...props}
      >
        {children}
        <Primitiva.Close
          className="absolute right-3 top-3 rounded p-1.5 text-tinta-tenue transition-colors hover:bg-superficie-tenue hover:text-tinta"
          aria-label="Cerrar"
        >
          <X className="size-4" />
        </Primitiva.Close>
      </Primitiva.Content>
    </Primitiva.Portal>
  )
}

export function CabeceraDialogo({
  titulo,
  descripcion,
}: {
  titulo: ReactNode
  descripcion?: ReactNode
}) {
  return (
    <div className="border-b border-borde px-5 py-4 pr-12">
      <Primitiva.Title className="text-base font-semibold leading-tight text-tinta">
        {titulo}
      </Primitiva.Title>
      {descripcion ? (
        <Primitiva.Description asChild>
          <div className="mt-1.5 text-[0.8125rem] leading-relaxed text-tinta-tenue">
            {descripcion}
          </div>
        </Primitiva.Description>
      ) : null}
    </div>
  )
}

export function PieDialogo({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-wrap justify-end gap-2 border-t border-borde bg-superficie-tenue/60 px-5 py-3.5">
      {children}
    </div>
  )
}

export const CerrarDialogo = Primitiva.Close
