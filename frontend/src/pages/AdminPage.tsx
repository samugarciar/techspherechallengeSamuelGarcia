import { RefreshCw, Unplug, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'

import { api, MODO_SIMULADO } from '@/api'
import { simularCaidaDeConexion } from '@/api/mock'
import { CajaRag } from '@/components/admin/CajaRag'
import { DialogoBorrado } from '@/components/admin/DialogoBorrado'
import { IndicadorConexion } from '@/components/admin/IndicadorConexion'
import { PanelDetalle } from '@/components/admin/PanelDetalle'
import { TablaDocumentos } from '@/components/admin/TablaDocumentos'
import { ZonaSubida } from '@/components/admin/ZonaSubida'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import { CabeceraTarjeta, Tarjeta } from '@/components/ui/superficie'
import { useDocumentos } from '@/hooks/useDocumentos'
import { useFlujoDocumentos } from '@/hooks/useFlujoDocumentos'
import { useToken } from '@/hooks/useToken'
import { formatearNumero, pluralizar } from '@/lib/formato'
import type { Documento } from '@/types/api'

/**
 * Consola de documentos.
 *
 * La pantalla cuenta una sola historia de arriba abajo: se sube un documento, se
 * ve cómo el sistema lo aprende, se comprueba que lo sabe, se borra y se comprueba
 * que lo ha olvidado. Todo lo que no sirve a ese relato está fuera a propósito.
 */
export function AdminPage() {
  const token = useToken()
  // Sin token no se conecta nada: un `EventSource` contra un 401 reintentaría en
  // bucle y llenaría el log del backend de ruido antes de la demo.
  const habilitado = MODO_SIMULADO || token !== ''

  const documentos = useDocumentos(habilitado)
  const conexion = useFlujoDocumentos(
    {
      onDocumento: documentos.aplicarEvento,
      onEliminado: (evento) => documentos.aplicarEliminado(evento.id),
      onResincronizar: documentos.resincronizar,
    },
    habilitado,
  )

  const [detalle, setDetalle] = useState<Documento | null>(null)
  const [aBorrar, setABorrar] = useState<Documento | null>(null)

  const confirmarBorrado = useCallback(
    (documento: Documento) => documentos.eliminar(documento),
    [documentos],
  )

  const resumen = documentos.resumen
  const subtitulo = useMemo(() => {
    if (!habilitado) return 'Configura el token de administrador para empezar.'
    if (resumen.total === 0) return 'El agente todavía no conoce ningún documento.'
    const partes = [`${pluralizar(resumen.listos, 'documento')} en uso`]
    if (resumen.enCurso > 0) partes.push(`${formatearNumero(resumen.enCurso)} procesándose`)
    if (resumen.fallidos > 0) partes.push(`${formatearNumero(resumen.fallidos)} con error`)
    partes.push(`${pluralizar(resumen.trozos, 'fragmento')} que el agente puede citar`)
    return partes.join(' · ')
  }, [habilitado, resumen])

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-tinta">Base de conocimiento</h1>
        <p className="mt-1 text-[0.8125rem] text-tinta-tenue">{subtitulo}</p>
      </div>

      {!habilitado ? (
        <Aviso tono="atencion" titulo="Falta el token de administrador">
          Todas las rutas de la API salvo `/api/health` piden la cabecera `X-Admin-Token`. Pégalo en
          «Configurar token», arriba a la derecha, y la consola se conectará sola.
        </Aviso>
      ) : null}

      {documentos.error ? (
        <Aviso
          tono="error"
          titulo="No se pudo cargar la lista de documentos"
          acciones={
            <Boton variante="contorno" tamano="sm" onClick={() => void documentos.cargar()}>
              <RefreshCw />
              Reintentar
            </Boton>
          }
        >
          {documentos.error}
        </Aviso>
      ) : null}

      <Tarjeta>
        <CabeceraTarjeta
          titulo="Añadir documentos"
          descripcion="Protocolos, guías de alta y pautas de cuidados. En cuanto termine la ingesta, el agente podrá citarlos en una llamada."
        />
        <div className="px-5 py-4">
          <ZonaSubida
            onSubir={(archivos) => void documentos.subir(archivos)}
            subidas={documentos.subidas}
            onDescartarSubida={documentos.descartarSubida}
            deshabilitado={!habilitado}
            motivoDeshabilitado="Configura primero el token de administrador."
          />
        </div>
      </Tarjeta>

      {documentos.olvidoReciente ? (
        <Aviso
          tono="exito"
          titulo="Olvidado"
          className="entrando"
          acciones={
            <Boton
              variante="fantasma"
              tamano="iconoSm"
              onClick={documentos.descartarOlvido}
              aria-label="Descartar aviso"
            >
              <X />
            </Boton>
          }
        >
          <span className="font-medium text-tinta">{documentos.olvidoReciente.nombre}</span> ya no
          está en la base de conocimiento. Se eliminaron{' '}
          <span className="font-medium text-tinta">
            {pluralizar(documentos.olvidoReciente.chunks, 'fragmento')}
          </span>{' '}
          con sus embeddings, en la misma transacción. Pregúntale ahora al agente por ese contenido:
          ya no tiene evidencia.
        </Aviso>
      ) : null}

      <Tarjeta>
        <CabeceraTarjeta
          titulo={`Documentos (${formatearNumero(resumen.total)})`}
          descripcion="El estado se actualiza solo por SSE; no hace falta recargar la página."
          acciones={
            <>
              <IndicadorConexion estado={conexion.estado} intento={conexion.intento} />
              {MODO_SIMULADO ? (
                <Boton
                  variante="fantasma"
                  tamano="sm"
                  onClick={simularCaidaDeConexion}
                  title="Corta el flujo simulado para ver la reconexión con backoff"
                >
                  <Unplug />
                  Simular caída
                </Boton>
              ) : null}
              <Boton
                variante="contorno"
                tamano="sm"
                disabled={!habilitado}
                onClick={() => void documentos.cargar()}
              >
                <RefreshCw />
                Recargar
              </Boton>
            </>
          }
        />
        <TablaDocumentos
          documentos={documentos.documentos}
          cargando={documentos.cargando && habilitado}
          recienAprendidos={documentos.recienAprendidos}
          borrando={documentos.borrando}
          onVerDetalle={setDetalle}
          onBorrar={setABorrar}
        />
      </Tarjeta>

      {/* Verificación del propio panel. Quitar esta línea la elimina entera. */}
      <CajaRag />

      <PanelDetalle
        documento={detalle}
        abierto={detalle !== null}
        onCambiarApertura={(abierto) => {
          if (!abierto) setDetalle(null)
        }}
      />

      <DialogoBorrado
        documento={aBorrar}
        abierto={aBorrar !== null}
        borrando={documentos.borrando !== null}
        onCambiarApertura={(abierto) => {
          if (!abierto) setABorrar(null)
        }}
        onConfirmar={confirmarBorrado}
      />

      <PieInformativo />
    </div>
  )
}

function PieInformativo() {
  return (
    <p className="pb-4 text-xs leading-relaxed text-tinta-tenue">
      {MODO_SIMULADO
        ? 'Modo simulado: los documentos y las respuestas del RAG los genera un backend en memoria (src/api/mock). La máquina de estados, los tiempos y las reglas de borrado son las del contrato.'
        : `Conectado a la API en ${api.esSimulado ? 'simulación' : 'localhost:8000'} mediante el proxy de desarrollo.`}
    </p>
  )
}
