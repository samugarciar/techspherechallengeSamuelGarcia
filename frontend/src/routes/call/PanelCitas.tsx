import { reloj } from '@/lib/duracion'
import { EtiquetaCita } from '@/routes/call/EtiquetaCita'
import type { CitaUsada } from '@/routes/call/estadoLlamada'

/**
 * Las citas que el agente va usando, según llegan.
 *
 * Existe además de las citas pegadas a cada frase porque responde a otra
 * pregunta: no «¿de dónde salió esto?» sino «¿sobre qué se ha apoyado esta
 * llamada entera?». Es la lista que un clínico querría ver al revisar, y la que
 * deja claro que el agente no está improvisando.
 */
export function PanelCitas({ citas }: { citas: CitaUsada[] }) {
  if (citas.length === 0) {
    return (
      <p className="px-5 py-6 text-[0.8125rem] leading-relaxed text-tinta-tenue">
        Todavía no ha hecho falta consultar ningún protocolo. En cuanto el agente responda algo
        clínico, aquí aparecerá el documento, la sección y la página de los que lo sacó.
      </p>
    )
  }

  return (
    <ol className="divide-y divide-borde">
      {citas.map((cita) => (
        <li key={cita.clave} className="flex items-center gap-3 px-5 py-2.5">
          <EtiquetaCita cita={cita} className="min-w-0" />
          <span className="numerico ml-auto shrink-0 text-xs text-tinta-tenue">
            {reloj(cita.instante)}
          </span>
        </li>
      ))}
    </ol>
  )
}
