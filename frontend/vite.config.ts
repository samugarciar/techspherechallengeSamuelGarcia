import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// El backend se consume por proxy en desarrollo (`/api` -> localhost:8000) en vez
// de por URL absoluta. Así el navegador ve una sola procedencia: ni CORS que
// configurar en FastAPI, ni el caso especial del SSE, que con CORS y credenciales
// da bastante más guerra que un fetch normal. En producción se sirve el build
// detrás del mismo dominio que la API, y todo sigue igual.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const destino = env.VITE_PROXY_TARGET || 'http://localhost:8000'

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: destino,
          changeOrigin: true,
          // Sin esto el SSE llega en bloques: el proxy no debe tamponar el flujo.
          configure: (proxy) => {
            proxy.on('proxyRes', (proxyRes) => {
              if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
                proxyRes.headers['cache-control'] = 'no-cache, no-transform'
              }
            })
          },
        },
      },
    },
  }
})
