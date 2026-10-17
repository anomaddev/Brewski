import { defineConfig } from 'vite'

const rawHost = process.env.BREWSKI_HOST || '127.0.0.1'
const apiHost = rawHost === '0.0.0.0' || rawHost === '::' ? '127.0.0.1' : rawHost
const apiPort = process.env.BREWSKI_PORT || '5000'

export default defineConfig({
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': `http://${apiHost}:${apiPort}`,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
