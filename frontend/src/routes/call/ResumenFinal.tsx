import { ArrowLeft, CheckCircle2, ListChecks, PhoneOff, ScrollText, Siren } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Boton } from '@/components/ui/boton'
import { CabeceraTarjeta, Tarjeta } from '@/components/ui/superficie'
import { formatearDuracion } from '@/lib/duracion'
import { formatearMs, pluralizar } from '@/lib/formato'
import { EtiquetaCita } from '@/routes/call/EtiquetaCita'
import { medianaEtapa, type EstadoConversacion } from '@/routes/call/estadoLlamada'
import { ETIQUETA_MOTIVO_FIN, ETIQUETA_URGENCIA } from '@/types/llamadas'
import type { LlamadaCreada, Paciente } from '@/types/llamadas'

/**
 * Qué quedó registrado al colgar.
 *
 * El contrato lo pide («resumen con lo registrado y si hubo escalamiento») y es
 * el cierre natural de la demo: se acaba de ver una conversación pasar, y aquí
 * está lo que el sistema se lleva de ella. Si hubo escalamiento, eso manda sobre
 * todo lo demás y va primero.
 */
export function ResumenFinal({
  paciente,
  llamada,
  conversacion,
  segundos,
  onVolver,
}: {
  paciente: Paciente | null
  llamada: LlamadaCreada | null
  conversacion: EstadoConversacion
  segundos: number
  onVolver: () => void
}) {
  const { bandera, citas, intervenciones, metricas, fin } = conversacion
  const turnosPaciente = intervenciones.filter((i) => i.quien === 'paciente').length
  const turnosAgente = intervenciones.filter((i) => i.quien === 'agente').length
  const medianaTotal = ['stt', 'retrieval', 'llm', 'tts'].reduce(
    (suma, etapa) => suma + (medianaEtapa(metricas, etapa as 'stt') ?? 0),
    0,
  )

  return (
    <div className="space-y-5">
      {bandera ? (
        <Tarjeta className="border-2 border-rojo bg-rojo-suave">
          <div className="flex flex-wrap items-start gap-4 px-5 py-4">
            <span className="flex size-11 shrink-0 items-center justify-center rounded-full bg-rojo text-white">
              <Siren className="size-6" aria-hidden />
            </span>
            <div className="min-w-0 flex-1">
              <h2 className="text-lg font-semibold leading-tight tracking-tight text-rojo">
                Escalado al equipo médico
              </h2>
              <p className="mt-1 text-[0.9375rem] font-medium text-tinta">{bandera.motivo}</p>
              <p className="mt-1.5 text-[0.8125rem] text-tinta-tenue">
                Urgencia {ETIQUETA_URGENCIA[bandera.urgencia].toLowerCase()}. Queda registrado en la
                llamada, con la transcripción y las citas que la fundamentan.
              </p>
            </div>
          </div>
        </Tarjeta>
      ) : (
        <Tarjeta className="border-verde/35 bg-verde-suave">
          <div className="flex flex-wrap items-start gap-4 px-5 py-4">
            <span className="flex size-11 shrink-0 items-center justify-center rounded-full bg-verde text-white">
              <CheckCircle2 className="size-6" aria-hidden />
            </span>
            <div className="min-w-0 flex-1">
              <h2 className="text-lg font-semibold leading-tight tracking-tight text-verde">
                Seguimiento sin signos de alarma
              </h2>
              <p className="mt-1 text-[0.8125rem] leading-relaxed text-tinta-tenue">
                No apareció ningún criterio de escalamiento. La llamada queda archivada con su
                transcripción por si alguien quiere revisarla.
              </p>
            </div>
          </div>
        </Tarjeta>
      )}

      <Tarjeta>
        <CabeceraTarjeta
          titulo={`Llamada a ${paciente?.nombre ?? 'paciente'}`}
          descripcion={
            fin
              ? `${ETIQUETA_MOTIVO_FIN[fin]} · ${formatearDuracion(segundos)}`
              : formatearDuracion(segundos)
          }
        />
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 px-5 py-4 sm:grid-cols-4">
          <Dato etiqueta="Duración" valor={formatearDuracion(segundos)} />
          <Dato etiqueta="Turnos" valor={`${turnosAgente} agente · ${turnosPaciente} paciente`} />
          <Dato etiqueta="Citas usadas" valor={pluralizar(citas.length, 'cita')} />
          <Dato
            etiqueta="Latencia mediana"
            valor={metricas.length > 0 ? formatearMs(medianaTotal) : '—'}
          />
        </dl>

        {conversacion.interrupciones > 0 ? (
          <p className="border-t border-borde px-5 py-3 text-[0.8125rem] text-tinta-tenue">
            El paciente interrumpió al agente{' '}
            <span className="font-medium text-tinta">
              {pluralizar(conversacion.interrupciones, 'vez', 'veces')}
            </span>
            . Se descartaron {formatearMs(conversacion.msDescartados)} de voz ya sintetizada que
            estaban esperando turno en el cliente.
          </p>
        ) : null}
      </Tarjeta>

      {citas.length > 0 ? (
        <Tarjeta>
          <CabeceraTarjeta
            titulo="Evidencia utilizada"
            descripcion="Todo lo que el agente afirmó de contenido clínico salió de aquí."
          />
          <ul className="flex flex-wrap gap-2 px-5 py-4">
            {citas.map((cita) => (
              <li key={cita.clave}>
                <EtiquetaCita cita={cita} />
              </li>
            ))}
          </ul>
        </Tarjeta>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Boton variante="contorno" onClick={onVolver}>
          <ArrowLeft />
          Volver a la lista
        </Boton>
        {llamada ? (
          <Boton variante="secundario" asChild>
            <Link to={`/calls/${llamada.call_id}`}>
              <ScrollText />
              Ver la transcripción completa
            </Link>
          </Boton>
        ) : null}
        <Boton variante="fantasma" asChild>
          <Link to="/calls">
            <ListChecks />
            Historial de llamadas
          </Link>
        </Boton>
      </div>

      {intervenciones.length === 0 ? (
        <p className="flex items-center gap-2 text-[0.8125rem] text-tinta-tenue">
          <PhoneOff className="size-4 shrink-0" aria-hidden />
          La llamada terminó antes de que se dijera nada.
        </p>
      ) : null}
    </div>
  )
}

function Dato({ etiqueta, valor }: { etiqueta: string; valor: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-tinta-tenue">{etiqueta}</dt>
      <dd className="numerico mt-0.5 text-[0.9375rem] font-medium text-tinta">{valor}</dd>
    </div>
  )
}
