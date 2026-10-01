import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // Defaults to a backend started directly on the host. Set
        // VITE_API_TARGET=http://127.0.0.1:80 to develop the UI against the
        // running docker-compose stack instead (its backend port is internal,
        // so requests have to go in through Caddy).
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path,
      },
    },
  },
})
