import { ArrowLeft, RefreshCw, Siren } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'

import { MODO_SIMULADO } from '@/api'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import { Insignia } from '@/components/ui/insignia'
import { CabeceraTarjeta, Esqueleto, Tarjeta } from '@/components/ui/superficie'
import { useToken } from '@/hooks/useToken'
import { formatearDuracion } from '@/lib/duracion'
import { formatearFecha, pluralizar } from '@/lib/formato'
import { TurnosLlamada } from '@/routes/calls/TurnosLlamada'
import { useDetalleLlamada } from '@/routes/calls/useHistorial'
import { ETIQUETA_ESTADO_LLAMADA } from '@/types/llamadas'

/**
 * `/calls/:id` — una llamada, entera.
 *
 * Ruta propia y no un panel lateral: una llamada escalada es lo que alguien va a
 * querer enviar por mensaje a un compañero, y para eso hace falta un enlace.
 */
export function PaginaDetalleLlamada() {
  const { id } = useParams<{ id: string }>()
  const token = useToken()
  const habilitado = MODO_SIMULADO || token !== ''
  const { llamada, cargando, error, cargar } = useDetalleLlamada(id, habilitado)

  const citas = llamada?.turnos.reduce((suma, turno) => suma + turno.citas.length, 0) ?? 0

  return (
    <div className="space-y-5">
      <Boton variante="fantasma" tamano="sm" asChild className="-ml-3">
        <Link to="/calls">
          <ArrowLeft />
          Historial
        </Link>
      </Boton>

      {!habilitado ? (
        <Aviso tono="atencion" titulo="Falta el token de administrador">
          Pégalo en «Configurar token», arriba a la derecha, para poder abrir la llamada.
        </Aviso>
      ) : null}

      {error ? (
        <Aviso
          tono="error"
          titulo="No se pudo abrir la llamada"
          acciones={
            <Boton variante="contorno" tamano="sm" onClick={() => void cargar()}>
              <RefreshCw />
              Reintentar
            </Boton>
          }
        >
          {error}
        </Aviso>
      ) : null}

      {cargando && habilitado ? <Cargando /> : null}

      {llamada ? (
        <>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="min-w-0">
              <h1 className="truncate text-xl font-semibold tracking-tight text-tinta">
                {llamada.paciente}
              </h1>
              <p className="mt-1 text-[0.8125rem] text-tinta-tenue">
                {llamada.cirugia ?? 'Sin cirugía registrada'} · {formatearFecha(llamada.iniciada)}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Insignia color={llamada.estado === 'completada' ? 'verde' : 'gris'}>
                {ETIQUETA_ESTADO_LLAMADA[llamada.estado]}
              </Insignia>
            </div>
          </div>

          {llamada.escalada ? (
            <Tarjeta className="border-2 border-rojo bg-rojo-suave">
              <div className="flex flex-wrap items-start gap-4 px-5 py-4">
                <span className="flex size-11 shrink-0 items-center justify-center rounded-full bg-rojo text-white">
                  <Siren className="size-6" aria-hidden />
                </span>
                <div className="min-w-0 flex-1">
                  <h2 className="text-lg font-semibold leading-tight tracking-tight text-rojo">
                    Escalada al equipo médico
                  </h2>
                  <p className="mt-1 text-[0.9375rem] font-medium text-tinta">
                    {llamada.motivo_escalada ?? 'Se detectó un signo de alarma durante la llamada.'}
                  </p>
                  <p className="mt-1.5 text-[0.8125rem] leading-relaxed text-tinta-tenue">
                    El turno marcado como «Sistema» en la transcripción es el momento exacto en que
                    saltó el detector. A partir de ahí el agente abandonó el cuestionario.
                  </p>
                </div>
              </div>
            </Tarjeta>
          ) : null}

          <Tarjeta>
            <CabeceraTarjeta
              titulo="Transcripción completa"
              descripcion={
                `${pluralizar(llamada.turnos.length, 'turno')} · ` +
                `${pluralizar(citas, 'cita')} · ${formatearDuracion(llamada.duracion_s)}`
              }
            />
            <TurnosLlamada turnos={llamada.turnos} />
          </Tarjeta>
        </>
      ) : null}
    </div>
  )
}

function Cargando() {
  return (
    <div className="space-y-3">
      <Esqueleto className="h-8 w-64" />
      <Esqueleto className="h-24 w-full" />
      <Esqueleto className="h-24 w-full" />
      <p className="sr-only">Cargando la llamada…</p>
    </div>
  )
}
