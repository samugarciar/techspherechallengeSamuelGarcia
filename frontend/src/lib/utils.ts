import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Une clases y deja ganar a la última cuando dos compiten por la misma propiedad. */
export function cn(...entradas: ClassValue[]) {
  return twMerge(clsx(entradas))
}
