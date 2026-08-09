import { AlertOctagon } from 'lucide-react'

import { reloj } from '@/lib/duracion'
import { cn } from '@/lib/utils'
import type { BanderaRoja } from '@/routes/call/estadoLlamada'
import { ETIQUETA_URGENCIA } from '@/types/llamadas'

/**
 * El momento clínico de la demo.
 *
 * El cambio tiene que ser inequívoco desde el fondo de la sala, y por eso ocupa
 * el ancho entero y usa el rojo de estado que en el resto de la aplicación no
 * aparece nunca salvo para un error. Pero es un producto médico: tres pulsos y
 * se queda quieto, sin parpadeos perpetuos ni sonidos. Quien está escuchando la
 * llamada tiene que poder seguir escuchándola.
 *
 * El texto explica lo que el agente está HACIENDO —decisión 2 del contrato:
 * abandona las preguntas, da la instrucción, confirma que se ha entendido y
 * cierra— porque si no, un cartel rojo sólo dice «algo va mal» y deja al jurado
 * preguntándose si el sistema ha reaccionado o simplemente se ha asustado.
 */
export function AvisoBanderaRoja({
  bandera,
  animar = true,
}: {
  bandera: BanderaRoja
  animar?: boolean
}) {
  return (
    <section
      role="alert"
      className={cn(
        'overflow-hidden rounded-consola border-2 border-rojo bg-rojo-suave',
        animar && 'alarma',
      )}
    >
      <div className={cn('h-1 w-full bg-rojo', animar && 'barra-alarma')} aria-hidden />

      <div className="flex flex-wrap items-start gap-4 px-5 py-4">
        <span className="flex size-11 shrink-0 items-center justify-center rounded-full bg-rojo text-white">
          <AlertOctagon className="size-6" aria-hidden />
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold leading-tight tracking-tight text-rojo">
              Signo de alarma detectado
            </h2>
            <span className="rounded-full border border-rojo/40 bg-superficie px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide text-rojo">
              {ETIQUETA_URGENCIA[bandera.urgencia]}
            </span>
            <span className="numerico ml-auto text-xs text-tinta-tenue">
              minuto {reloj(bandera.instante)}
            </span>
          </div>

          <p className="mt-1.5 text-[0.9375rem] font-medium leading-snug text-tinta">
            {bandera.motivo}
          </p>

          <p className="mt-2 text-[0.8125rem] leading-relaxed text-tinta-tenue">
            El agente ha abandonado las preguntas que quedaban. Da la instrucción de urgencia del
            protocolo, comprueba que el paciente la ha entendido, registra el escalamiento y cierra
            la llamada.
          </p>
        </div>
      </div>
    </section>
  )
}
