import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * Último cortafuegos contra la pantalla en blanco.
 *
 * Si un componente revienta al renderizar —un campo que el backend devolvió con
 * otra forma, por ejemplo— React desmonta el árbol entero y deja el `body` vacío.
 * En una demo eso es indistinguible de «la aplicación no arranca». Con esto, el
 * peor caso es un mensaje en español y un botón de recargar.
 */
interface Estado {
  error: Error | null
}

export class LimiteDeError extends Component<{ children: ReactNode }, Estado> {
  override state: Estado = { error: null }

  static getDerivedStateFromError(error: Error): Estado {
    return { error }
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Fallo al renderizar la consola', error, info.componentStack)
  }

  override render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="mx-auto flex min-h-screen max-w-lg flex-col justify-center gap-4 px-6">
        <h1 className="text-lg font-semibold text-tinta">La consola se ha detenido</h1>
        <p className="text-sm leading-relaxed text-tinta-tenue">
          Algo falló al dibujar la pantalla. No se ha perdido nada: los documentos siguen en el
          servidor. Recarga la página para volver a intentarlo.
        </p>
        <pre className="overflow-x-auto rounded-consola border border-borde bg-superficie-tenue p-3 font-mono text-xs text-tinta-tenue">
          {error.message}
        </pre>
        <div>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-consola bg-primario px-4 py-2 text-sm font-medium text-primario-tinta"
          >
            Recargar la consola
          </button>
        </div>
      </div>
    )
  }
}
