import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const appVersion = readFileSync(resolve(repoRoot, 'VERSION'), 'utf8').trim()

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  ...(mode === 'android' ? { build: { target: 'chrome91' } } : {}),
}))
