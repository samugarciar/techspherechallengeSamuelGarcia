import { Phone, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'

import { MODO_SIMULADO } from '@/api'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import { CabeceraTarjeta, Tarjeta } from '@/components/ui/superficie'
import { useToken } from '@/hooks/useToken'
import { pluralizar } from '@/lib/formato'
import { TablaLlamadas } from '@/routes/calls/TablaLlamadas'
import { useHistorial } from '@/routes/calls/useHistorial'

/**
 * `/calls` — historial.
 *
 * Es lo que hace el sistema auditable en contexto clínico: una llamada que no se
 * puede releer después, con lo que se dijo y en qué se basó, no vale para nada
 * en un hospital.
 */
export function PaginaHistorial() {
  const token = useToken()
  const habilitado = MODO_SIMULADO || token !== ''
  const historial = useHistorial(habilitado)

  const escaladas = historial.llamadas.filter((llamada) => llamada.escalada).length

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-tinta">Historial de llamadas</h1>
          <p className="mt-1 text-[0.8125rem] text-tinta-tenue">
            {historial.llamadas.length === 0
              ? 'Aquí queda registrada cada llamada, turno a turno.'
              : `${pluralizar(historial.llamadas.length, 'llamada')} registradas` +
                (escaladas > 0 ? ` · ${escaladas} con escalamiento` : '')}
          </p>
        </div>
        <Boton variante="contorno" asChild>
          <Link to="/call">
            <Phone />
            Nueva llamada
          </Link>
        </Boton>
      </div>

      {!habilitado ? (
        <Aviso tono="atencion" titulo="Falta el token de administrador">
          El historial pide la cabecera `X-Admin-Token`. Pégalo en «Configurar token», arriba a la
          derecha.
        </Aviso>
      ) : null}

      {historial.error ? (
        <Aviso
          tono="error"
          titulo="No se pudo cargar el historial"
          acciones={
            <Boton variante="contorno" tamano="sm" onClick={() => void historial.cargar()}>
              <RefreshCw />
              Reintentar
            </Boton>
          }
        >
          {historial.error}
        </Aviso>
      ) : null}

      <Tarjeta>
        <CabeceraTarjeta
          titulo="Llamadas registradas"
          descripcion="Abre una para ver la transcripción completa con las citas de cada respuesta."
          acciones={
            <Boton
              variante="contorno"
              tamano="sm"
              disabled={!habilitado}
              onClick={() => void historial.cargar()}
            >
              <RefreshCw />
              Recargar
            </Boton>
          }
        />
        <TablaLlamadas llamadas={historial.llamadas} cargando={historial.cargando && habilitado} />
      </Tarjeta>
    </div>
  )
}
