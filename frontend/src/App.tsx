import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { Disposicion } from '@/components/Disposicion'
import { LimiteDeError } from '@/components/LimiteDeError'
import { AdminPage } from '@/pages/AdminPage'
import { NoEncontrada } from '@/pages/NoEncontrada'

/**
 * Enrutado.
 *
 * Sólo existe `/admin` porque la consola cubre por ahora sólo la gestión de
 * documentos. La estructura ya admite las vistas que faltan —cada una es una
 * línea más dentro de `Disposicion`— y por eso el layout está separado de la
 * página desde el primer día:
 *
 *   <Route path="/pacientes" element={<PacientesPage />} />
 *   <Route path="/llamadas"  element={<LlamadasPage />} />
 *   <Route path="/trazas"    element={<TrazasPage />} />
 *   <Route path="/call"      element={<LlamadaPage />} />   (Fase 5, fuera del layout)
 */
export function App() {
  return (
    <LimiteDeError>
      <BrowserRouter>
        <Routes>
          <Route element={<Disposicion />}>
            <Route path="/" element={<Navigate to="/admin" replace />} />
            <Route path="/admin" element={<AdminPage />} />
            <Route path="*" element={<NoEncontrada />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LimiteDeError>
  )
}
