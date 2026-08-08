import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'

const base =
  'w-full rounded-consola border border-borde bg-superficie px-3 text-sm text-tinta placeholder:text-tinta-tenue/70 transition-colors focus-visible:border-anillo disabled:cursor-not-allowed disabled:opacity-60'

export function Campo({ className, ...props }: ComponentProps<'input'>) {
  return <input className={cn(base, 'h-9', className)} {...props} />
}

export function AreaTexto({ className, ...props }: ComponentProps<'textarea'>) {
  return <textarea className={cn(base, 'min-h-20 py-2 leading-relaxed', className)} {...props} />
}

export function Etiqueta({ className, ...props }: ComponentProps<'label'>) {
  return (
    <label
      className={cn('text-xs font-medium uppercase tracking-wide text-tinta-tenue', className)}
      {...props}
    />
  )
}
