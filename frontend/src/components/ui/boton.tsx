import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'

const variantes = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-consola text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg]:size-4",
  {
    variants: {
      variante: {
        primario: 'bg-primario text-primario-tinta hover:bg-primario/90',
        secundario: 'bg-superficie-tenue text-tinta border border-borde hover:bg-borde/40',
        contorno: 'border border-borde-fuerte bg-superficie text-tinta hover:bg-superficie-tenue',
        fantasma: 'text-tinta-tenue hover:bg-superficie-tenue hover:text-tinta',
        destructivo: 'bg-rojo text-white hover:bg-rojo/90',
        enlace: 'text-primario underline-offset-4 hover:underline',
      },
      tamano: {
        sm: 'h-8 px-3 text-[0.8125rem]',
        md: 'h-9 px-4',
        lg: 'h-11 px-6 text-base',
        icono: 'h-9 w-9',
        iconoSm: 'h-8 w-8',
      },
    },
    defaultVariants: { variante: 'primario', tamano: 'md' },
  },
)

export type PropsBoton = ComponentProps<'button'> &
  VariantProps<typeof variantes> & { asChild?: boolean }

export function Boton({ className, variante, tamano, asChild = false, ...props }: PropsBoton) {
  const Componente = asChild ? Slot : 'button'
  return <Componente className={cn(variantes({ variante, tamano }), className)} {...props} />
}
