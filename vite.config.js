import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api/proxy': {
        target: 'http://178.104.133.71:8000',
        changeOrigin: true,
        headers: { 'x-api-key': 'JPxK9m2026TraderB0t!' },
        rewrite: path => {
          const m = path.match(/[?&]path=([^&]+)/)
          return m ? decodeURIComponent(m[1]) : '/'
        },
      },
    },
  },
})
