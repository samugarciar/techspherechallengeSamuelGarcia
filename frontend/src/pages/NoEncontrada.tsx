import { Link } from 'react-router-dom'

export function NoEncontrada() {
  return (
    <div className="py-20 text-center">
      <p className="text-sm font-medium text-tinta">Esa página no existe</p>
      <p className="mx-auto mt-1 max-w-sm text-[0.8125rem] leading-relaxed text-tinta-tenue">
        La consola cubre por ahora la gestión de documentos. El resto de secciones llegará en las
        siguientes fases.
      </p>
      <Link
        to="/admin"
        className="mt-4 inline-block rounded-consola bg-primario px-4 py-2 text-sm font-medium text-primario-tinta"
      >
        Ir a Documentos
      </Link>
    </div>
  )
}
