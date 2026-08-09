import { FileText } from 'lucide-react'

import { cn } from '@/lib/utils'
import type { Cita } from '@/types/llamadas'

/**
 * Una cita, en una línea.
 *
 * Es la unidad de auditoría del sistema: dice de qué documento, de qué sección y
 * de qué página salió lo que el agente acaba de afirmar. Se pinta pequeña y
 * monoespaciada a propósito —no compite con la frase que fundamenta— pero
 * siempre visible: una afirmación clínica sin origen es exactamente lo que este
 * proyecto promete no hacer.
 */
export function EtiquetaCita({ cita, className }: { cita: Cita; className?: string }) {
  const partes = [cita.filename]
  if (cita.heading) partes.push(cita.heading)
  if (cita.page !== null) partes.push(`p. ${cita.page}`)

  return (
    <span
      className={cn(
        'inline-flex max-w-full items-center gap-1.5 rounded-full border border-borde bg-superficie px-2.5 py-0.5 text-[0.6875rem] text-tinta-tenue',
        className,
      )}
      title={partes.join(' › ')}
    >
      <FileText className="size-3 shrink-0" aria-hidden />
      <span className="truncate font-mono">{partes.join(' › ')}</span>
    </span>
  )
}
