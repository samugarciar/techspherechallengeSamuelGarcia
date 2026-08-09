/**
 * Reproducción del audio del agente.
 *
 * Adaptado de `scripts/spikes/cliente_voz/index.html`, que es donde esto se
 * resolvió con micrófono de verdad. La idea, tal cual: los trozos llegan del
 * servidor mucho más rápido que el tiempo real, así que no se reproducen «según
 * llegan» sino que se PROGRAMAN en la línea de tiempo del AudioContext, uno
 * detrás de otro.
 *
 * De ahí sale la parte que casi nadie implementa y sin la cual el barge-in no
 * existe: cuando el servidor manda `parar`, ya hay varios segundos de voz
 * programados en el cliente (se midieron 5,4 s en el spike). Cortar sólo en el
 * servidor no calla al agente. `vaciar()` cancela lo programado, y eso es lo que
 * hace que la interrupción se sienta real.
 */
export class ReproductorAgente {
  private ctx: AudioContext | null = null
  private salida: GainNode | null = null
  private analizador: AnalyserNode | null = null
  private muestras: Uint8Array<ArrayBuffer> | null = null
  /** Instante del AudioContext en el que sonará el siguiente trozo. */
  private siguiente = 0
  private fuentes = new Set<AudioBufferSourceNode>()
  private sampleRate = 24_000

  /** El `sample_rate_salida` lo dice el servidor en `listo`. */
  configurar(sampleRate: number | undefined): void {
    if (typeof sampleRate === 'number' && sampleRate > 0) this.sampleRate = sampleRate
  }

  /**
   * El AudioContext se crea tarde a propósito: los navegadores sólo lo dejan
   * arrancar dentro de un gesto del usuario, y aquí ese gesto es el clic que
   * inicia la llamada.
   */
  private asegurarContexto(): AudioContext {
    if (this.ctx) return this.ctx
    const ctx = new AudioContext()
    const salida = ctx.createGain()
    const analizador = ctx.createAnalyser()
    analizador.fftSize = 512
    salida.connect(analizador)
    analizador.connect(ctx.destination)
    this.ctx = ctx
    this.salida = salida
    this.analizador = analizador
    this.muestras = new Uint8Array(new ArrayBuffer(analizador.fftSize))
    return ctx
  }

  encolar(pcm: Int16Array): void {
    if (pcm.length === 0) return
    const ctx = this.asegurarContexto()
    if (ctx.state === 'suspended') void ctx.resume()

    const buffer = ctx.createBuffer(1, pcm.length, this.sampleRate)
    const canal = buffer.getChannelData(0)
    for (let i = 0; i < pcm.length; i += 1) canal[i] = (pcm[i] ?? 0) / 32768

    const fuente = ctx.createBufferSource()
    fuente.buffer = buffer
    fuente.connect(this.salida ?? ctx.destination)
    // Si la cola se ha agotado, arrancar en `currentTime` y no en un instante ya
    // pasado: programar en el pasado hace que el trozo suene entero de golpe.
    this.siguiente = Math.max(this.siguiente, ctx.currentTime)
    fuente.start(this.siguiente)
    this.siguiente += buffer.duration
    this.fuentes.add(fuente)
    fuente.onended = () => this.fuentes.delete(fuente)
  }

  /** Milisegundos de voz del agente pendientes de sonar. */
  msEnCola(): number {
    if (!this.ctx) return 0
    return Math.max(0, (this.siguiente - this.ctx.currentTime) * 1000)
  }

  /** Corta todo lo programado. Devuelve cuántos ms de audio se han descartado. */
  vaciar(): number {
    const descartados = this.msEnCola()
    for (const fuente of this.fuentes) {
      try {
        fuente.onended = null
        fuente.stop()
      } catch {
        // Una fuente que ya terminó lanza al pararla; da igual, se va igualmente.
      }
    }
    this.fuentes.clear()
    this.siguiente = this.ctx?.currentTime ?? 0
    return descartados
  }

  /** Nivel de salida 0–1, para que el indicador «hablando» respire de verdad. */
  nivel(): number {
    const analizador = this.analizador
    const muestras = this.muestras
    if (!analizador || !muestras) return 0
    analizador.getByteTimeDomainData(muestras)
    let suma = 0
    for (let i = 0; i < muestras.length; i += 1) {
      const v = ((muestras[i] ?? 128) - 128) / 128
      suma += v * v
    }
    return Math.min(1, Math.sqrt(suma / muestras.length) * 3)
  }

  async cerrar(): Promise<void> {
    this.vaciar()
    const ctx = this.ctx
    this.ctx = null
    this.salida = null
    this.analizador = null
    this.muestras = null
    if (ctx) await ctx.close().catch(() => undefined)
  }
}
