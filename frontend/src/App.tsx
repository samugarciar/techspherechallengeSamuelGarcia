import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { Disposicion } from '@/components/Disposicion'
import { LimiteDeError } from '@/components/LimiteDeError'
import { AdminPage } from '@/pages/AdminPage'
import { NoEncontrada } from '@/pages/NoEncontrada'
import { PaginaLlamada } from '@/routes/call/PaginaLlamada'
import { PaginaDetalleLlamada } from '@/routes/calls/PaginaDetalleLlamada'
import { PaginaHistorial } from '@/routes/calls/PaginaHistorial'

/**
 * Enrutado.
 *
 * `/admin` es la consola de documentos; `/call` y `/calls` son la Fase 5. Las
 * tres comparten `Disposicion` a propósito: es la misma aplicación, y meter la
 * pantalla de llamada en un marco propio la habría convertido en otro producto
 * pegado al lado.
 *
 * Quedan pendientes, cada una a una línea de aquí:
 *
 *   <Route path="/pacientes" element={<PacientesPage />} />
 *   <Route path="/trazas"    element={<TrazasPage />} />
 */
export function App() {
  return (
    <LimiteDeError>
      <BrowserRouter>
        <Routes>
          <Route element={<Disposicion />}>
            <Route path="/" element={<Navigate to="/admin" replace />} />
            <Route path="/admin" element={<AdminPage />} />
            <Route path="/call" element={<PaginaLlamada />} />
            <Route path="/calls" element={<PaginaHistorial />} />
            <Route path="/calls/:id" element={<PaginaDetalleLlamada />} />
            <Route path="*" element={<NoEncontrada />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LimiteDeError>
  )
}
