import { useState } from 'react'
import {
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  FileCode,
  FileText,
  Layers,
  Search,
  Sparkles,
} from 'lucide-react'

import { reloj } from '@/lib/duracion'
import type { CitaUsada } from '@/routes/call/estadoLlamada'
import type { FaseLlamada } from '@/types/llamadas'

function iconoExtension(filename: string) {
  if (filename.endsWith('.pdf')) return <FileText className="size-4 text-rojo" aria-hidden />
  if (filename.endsWith('.md') || filename.endsWith('.txt'))
    return <FileCode className="size-4 text-primario" aria-hidden />
  return <BookOpen className="size-4 text-ambar" aria-hidden />
}

export function PanelCitas({
  citas,
  fase,
}: {
  citas: CitaUsada[]
  fase?: FaseLlamada | null
}) {
  const [expandida, setExpandida] = useState<string | null>(null)

  return (
    <div className="space-y-3">
      {/* Trazabilidad RAG Visual Pipeline */}
      <div className="border-b border-borde bg-superficie-tenue/60 px-5 py-3">
        <div className="flex items-center justify-between gap-2 text-[0.75rem] font-medium text-tinta-tenue">
          <span className="flex items-center gap-1.5 font-semibold text-tinta">
            <Layers className="size-3.5 text-primario" aria-hidden />
            Trazabilidad RAG
          </span>
          <span className="flex items-center gap-1 text-[0.6875rem] text-verde font-semibold">
            <CheckCircle2 className="size-3" aria-hidden />
            Grounding Activo
          </span>
        </div>

        {/* Flujo de etapas RAG */}
        <div className="mt-2.5 grid grid-cols-3 gap-1.5 text-center text-[0.6875rem]">
          <div className="rounded-md border border-borde bg-superficie px-2 py-1.2 shadow-2xs">
            <span className="block font-medium text-tinta">1. Búsqueda</span>
            <span className="block text-[0.625rem] text-tinta-tenue">bge-m3 + BM25</span>
          </div>
          <div className="rounded-md border border-borde bg-superficie px-2 py-1.2 shadow-2xs">
            <span className="block font-medium text-tinta">2. Reranker</span>
            <span className="block text-[0.625rem] text-tinta-tenue">Cross-Encoder</span>
          </div>
          <div className="rounded-md border border-primario/30 bg-primario-suave/40 px-2 py-1.2 text-primario shadow-2xs">
            <span className="block font-semibold">3. Evidencia</span>
            <span className="block text-[0.625rem] opacity-80">Citas por turno</span>
          </div>
        </div>

        {/* Estado de búsqueda en vivo */}
        {fase === 'pensando' ? (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-ambar/30 bg-ambar-suave/50 px-3 py-1.5 text-xs text-ambar">
            <Search className="size-3.5 animate-spin" aria-hidden />
            <span className="font-medium">Consultando corpus clínico en tiempo real...</span>
          </div>
        ) : null}
      </div>

      {/* Lista de citas / evidencia */}
      {citas.length === 0 ? (
        <div className="px-5 py-6 text-center text-[0.8125rem] leading-relaxed text-tinta-tenue">
          <p className="font-medium text-tinta">Sin evidencia clínica requerida aún</p>
          <p className="mt-1 text-xs text-tinta-tenue">
            En cuanto el paciente realice una pregunta clínica, aquí aparecerá la evidencia del
            protocolo clínico con su documento, sección y página de origen.
          </p>
        </div>
      ) : (
        <ol className="divide-y divide-borde px-2">
          {citas.map((cita) => {
            const estaExpandida = expandida === cita.clave
            return (
              <li key={cita.clave} className="group rounded-lg p-2.5 transition-colors hover:bg-superficie-tenue">
                <div
                  className="flex cursor-pointer items-start gap-3"
                  onClick={() => setExpandida(estaExpandida ? null : cita.clave)}
                >
                  <div className="mt-0.5 shrink-0 rounded-md border border-borde bg-superficie p-1.5 shadow-2xs">
                    {iconoExtension(cita.filename)}
                  </div>

                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-xs font-semibold text-tinta">
                        {cita.filename}
                      </span>
                      <span className="inline-flex items-center gap-0.5 rounded-full border border-verde/30 bg-verde-suave px-1.5 py-0.2 text-[0.625rem] font-medium text-verde">
                        <Sparkles className="size-2.5" aria-hidden />
                        Fundamentado
                      </span>
                    </div>

                    <div className="flex flex-wrap items-center gap-1.5 text-[0.6875rem]">
                      {cita.heading ? (
                        <span className="inline-flex items-center rounded-md border border-borde bg-superficie px-2 py-0.5 font-medium text-tinta-tenue">
                          📍 {cita.heading}
                        </span>
                      ) : null}
                      {cita.page !== null ? (
                        <span className="inline-flex items-center rounded-md border border-borde bg-superficie px-2 py-0.5 font-mono text-tinta-tenue">
                          📄 Pág. {cita.page}
                        </span>
                      ) : null}
                    </div>
                  </div>

                  <div className="flex flex-col items-end gap-1.5 shrink-0">
                    <span className="numerico text-[0.6875rem] font-medium tabular-nums text-tinta-tenue">
                      {reloj(cita.instante)}
                    </span>
                    <button
                      type="button"
                      className="text-tinta-tenue hover:text-tinta"
                      aria-label="Ver detalles de la cita"
                    >
                      {estaExpandida ? (
                        <ChevronUp className="size-3.5" />
                      ) : (
                        <ChevronDown className="size-3.5" />
                      )}
                    </button>
                  </div>
                </div>

                {/* Detalle desplegable de la cita */}
                {estaExpandida ? (
                  <div className="mt-2.5 rounded-md border border-borde bg-superficie p-2.5 text-xs text-tinta-tenue space-y-1 animate-in fade-in duration-150">
                    <p className="font-semibold text-tinta">Detalles de Trazabilidad:</p>
                    <p>• Archivo fuente: <span className="font-mono text-tinta">{cita.filename}</span></p>
                    {cita.heading ? <p>• Sección del documento: <span className="font-medium text-tinta">{cita.heading}</span></p> : null}
                    {cita.page ? <p>• Número de página: <span className="font-mono text-tinta">{cita.page}</span></p> : null}
                    <p>• Estado de verificación: <span className="font-medium text-verde">Grounding activo en base de datos PostgreSQL</span></p>
                  </div>
                ) : null}
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

