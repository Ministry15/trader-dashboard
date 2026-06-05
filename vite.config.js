import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      // Dev: emula o proxy serverless — extrai ?path= e faz forward para o VPS
      '/api/proxy': {
        target: 'http://178.104.133.71:80',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (proxyReq, req) => {
            const url = new URL(req.url, 'http://localhost')
            const vpsPath = url.searchParams.get('path') || '/'
            proxyReq.path = vpsPath
            proxyReq.setHeader('Authorization', 'Bearer JPxK9m2026TraderB0t!')
          })
        },
      },
    },
  },
})
