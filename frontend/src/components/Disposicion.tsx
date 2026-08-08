import { Activity } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'

import { MODO_SIMULADO } from '@/api'
import { ControlToken } from '@/components/admin/ControlToken'
import { cn } from '@/lib/utils'

/**
 * Marco de la consola.
 *
 * La navegación tiene una sola entrada porque la consola cubre por ahora sólo la
 * gestión de documentos. El array de abajo es el sitio donde entrarán pacientes,
 * llamadas y trazas cuando toque; enseñar hoy pestañas que no llevan a ninguna
 * parte sólo invita al jurado a pulsarlas.
 */
const SECCIONES = [{ camino: '/admin', etiqueta: 'Documentos' }]

export function Disposicion() {
  return (
    <div className="min-h-screen bg-fondo">
      <header className="sticky top-0 z-40 border-b border-borde bg-superficie/85 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="flex size-8 items-center justify-center rounded-consola bg-primario text-primario-tinta">
              <Activity className="size-4" aria-hidden />
            </span>
            <div className="min-w-0 leading-tight">
              <p className="truncate text-sm font-semibold text-tinta">
                Seguimiento postoperatorio
              </p>
              <p className="truncate text-xs text-tinta-tenue">Consola de administración</p>
            </div>
          </div>

          <nav className="flex items-center gap-1" aria-label="Secciones">
            {SECCIONES.map((seccion) => (
              <NavLink
                key={seccion.camino}
                to={seccion.camino}
                className={({ isActive }) =>
                  cn(
                    'rounded-consola px-3 py-1.5 text-sm transition-colors',
                    isActive
                      ? 'bg-primario-suave font-medium text-primario'
                      : 'text-tinta-tenue hover:bg-superficie-tenue hover:text-tinta',
                  )
                }
              >
                {seccion.etiqueta}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            {MODO_SIMULADO ? (
              <span
                className="rounded-full border border-ambar/35 bg-ambar-suave px-2.5 py-1 text-xs font-medium text-ambar"
                title="VITE_MOCK=1: los datos vienen del simulador en memoria, no del backend"
              >
                Datos simulados
              </span>
            ) : null}
            <ControlToken simulado={MODO_SIMULADO} />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
