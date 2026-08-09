/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** `1` para trabajar contra el backend simulado. Ver `src/api/mock/`. */
  readonly VITE_MOCK?: string
  /** Base de la API. Vacío = mismo origen (el proxy de Vite reenvía `/api`). */
  readonly VITE_API_BASE?: string
  /** Destino del proxy en desarrollo. Solo lo lee `vite.config.ts`. */
  readonly VITE_PROXY_TARGET?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
