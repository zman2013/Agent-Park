import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'fs'
import { resolve } from 'path'

const config = JSON.parse(
  readFileSync(resolve(__dirname, '..', 'config.json'), 'utf-8')
)

const backendPort = config.server?.port ?? 8001
const frontendPort = config.frontend?.port ?? 3000
const backendUrl = `http://127.0.0.1:${backendPort}`

export default defineConfig({
  plugins: [vue()],
  server: {
    port: frontendPort,
    proxy: {
      '/api': {
        target: backendUrl,
        changeOrigin: true,
      },
      '/ws': {
        target: backendUrl,
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
