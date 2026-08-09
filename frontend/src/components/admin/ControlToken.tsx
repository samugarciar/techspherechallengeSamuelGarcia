import { Check, KeyRound, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api'
import { mensajeDeError } from '@/api/errores'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import {
  CabeceraDialogo,
  ContenidoDialogo,
  Dialogo,
  DisparadorDialogo,
  PieDialogo,
} from '@/components/ui/dialogo'
import { Campo, Etiqueta } from '@/components/ui/campo'
import { useToken } from '@/hooks/useToken'
import { guardarToken } from '@/lib/token'
import { cn } from '@/lib/utils'

/**
 * Campo del token de administrador.
 *
 * Un administrador, sin sesiones ni JWT: está decidido en el contrato y en el
 * README. El valor se guarda en localStorage y viaja en `X-Admin-Token` (y por
 * query string en el SSE, que no admite cabeceras).
 *
 * El botón «Probar» existe porque un token mal pegado y un backend apagado se
 * parecen mucho desde la pantalla: llamando a `/api/health` —que no pide auth— y
 * después a un endpoint protegido, se distingue una cosa de la otra.
 */
type Prueba =
  | { estado: 'inactiva' }
  | { estado: 'probando' }
  | { estado: 'ok'; version: string; modelosListos: boolean }
  | { estado: 'fallo'; mensaje: string }

export function ControlToken({ simulado }: { simulado: boolean }) {
  const token = useToken()
  const [abierto, setAbierto] = useState(false)
  const [borrador, setBorrador] = useState(token)
  const [visible, setVisible] = useState(false)
  const [prueba, setPrueba] = useState<Prueba>({ estado: 'inactiva' })

  useEffect(() => {
    if (abierto) {
      setBorrador(token)
      setPrueba({ estado: 'inactiva' })
      setVisible(false)
    }
  }, [abierto, token])

  const configurado = token !== ''

  return (
    <Dialogo open={abierto} onOpenChange={setAbierto}>
      <DisparadorDialogo asChild>
        <Boton variante="contorno" tamano="sm" className="gap-2">
          <KeyRound
            className={cn(configurado || simulado ? 'text-verde' : 'text-ambar')}
            aria-hidden
          />
          {configurado ? 'Token guardado' : 'Configurar token'}
        </Boton>
      </DisparadorDialogo>

      <ContenidoDialogo className="w-[min(30rem,calc(100vw-2rem))]">
        <CabeceraDialogo
          titulo="Token de administrador"
          descripcion="Se envía en la cabecera X-Admin-Token de cada petición y por query string en el flujo de estado. Queda guardado en este navegador."
        />

        <div className="space-y-4 px-5 py-4">
          <div className="space-y-1.5">
            <Etiqueta htmlFor="campo-token">Token</Etiqueta>
            <div className="flex gap-2">
              <Campo
                id="campo-token"
                type={visible ? 'text' : 'password'}
                value={borrador}
                autoComplete="off"
                spellCheck={false}
                placeholder="Pega aquí el valor de ADMIN_TOKEN"
                onChange={(evento) => setBorrador(evento.target.value)}
                className="font-mono"
              />
              <Boton variante="contorno" tamano="sm" onClick={() => setVisible((v) => !v)}>
                {visible ? 'Ocultar' : 'Ver'}
              </Boton>
            </div>
            <p className="text-xs text-tinta-tenue">
              Es el mismo valor que `ADMIN_TOKEN` en el `.env` del backend.
            </p>
          </div>

          {simulado ? (
            <Aviso tono="info" titulo="Modo simulado activo">
              Con `VITE_MOCK=1` no hay backend detrás, así que el token no se comprueba. Sirve para
              dejarlo puesto antes de la demo real.
            </Aviso>
          ) : null}

          {prueba.estado === 'ok' ? (
            <Aviso tono="exito" titulo="Conexión correcta">
              El backend responde y acepta el token. Versión {prueba.version}.
              {/* La primera consulta al RAG en frío tarda 13 s medidos mientras
                  bge-m3 se carga. Decirlo aquí, antes de la demo, evita
                  descubrirlo delante del jurado. */}
              {prueba.modelosListos
                ? ' Los modelos del RAG ya están cargados.'
                : ' Los modelos del RAG todavía se están cargando: la primera consulta tardará unos segundos.'}
            </Aviso>
          ) : null}
          {prueba.estado === 'fallo' ? <Aviso tono="error">{prueba.mensaje}</Aviso> : null}
        </div>

        <PieDialogo>
          <Boton
            variante="contorno"
            disabled={prueba.estado === 'probando'}
            onClick={async () => {
              // Se guarda antes de probar: la capa HTTP lee el token del almacén,
              // no de este formulario.
              guardarToken(borrador)
              setPrueba({ estado: 'probando' })
              try {
                const salud = await api.salud()
                await api.listar({ limit: 1 })
                setPrueba({
                  estado: 'ok',
                  version: salud.version,
                  // Un backend que no publique el campo se trata como listo: no
                  // hay que asustar por una versión antigua del servidor.
                  modelosListos: salud.modelos_listos !== false,
                })
              } catch (causa) {
                setPrueba({ estado: 'fallo', mensaje: mensajeDeError(causa) })
              }
            }}
          >
            {prueba.estado === 'probando' ? <Loader2 className="animate-spin" /> : null}
            Probar conexión
          </Boton>
          <Boton
            onClick={() => {
              guardarToken(borrador)
              setAbierto(false)
            }}
          >
            <Check />
            Guardar
          </Boton>
        </PieDialogo>
      </ContenidoDialogo>
    </Dialogo>
  )
}
