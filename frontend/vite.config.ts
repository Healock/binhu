import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import legacy from '@vitejs/plugin-legacy'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const appVersion = readFileSync(resolve(repoRoot, 'VERSION'), 'utf8').trim()

export default defineConfig(({ mode }) => ({
  plugins: [
    react(),
    ...(mode === 'android'
      ? [legacy({
          targets: ['Chrome >= 51'],
          modernTargets: ['Chrome >= 61'],
          modernPolyfills: true,
        })]
      : []),
  ],
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
}))
