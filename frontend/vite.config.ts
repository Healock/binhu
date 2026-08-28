import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import legacy from '@vitejs/plugin-legacy'
import postcss from 'postcss'
import type { Plugin } from 'vite'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const appVersion = readFileSync(resolve(repoRoot, 'VERSION'), 'utf8').trim()

/**
 * Android WebView versions shipped on older devices do not understand CSS
 * cascade layers. Tailwind v4 emits its reset and utilities inside @layer,
 * which makes those rules disappear as a group. Flatten layers only for the
 * Android bundle; desktop builds keep the original stylesheet untouched.
 */
function androidCssCompatibility(): Plugin {
  return {
    name: 'android-css-compatibility',
    apply: 'build',
    async generateBundle(_options, bundle) {
      for (const asset of Object.values(bundle)) {
        if (asset.type !== 'asset' || !asset.fileName.endsWith('.css')) continue
        if (typeof asset.source !== 'string') continue

        const root = postcss.parse(asset.source)
        const flattenLayers = (container: { nodes?: postcss.ChildNode[] }) => {
          for (const node of [...(container.nodes || [])]) {
            if (node.type === 'atrule' && node.name === 'layer') {
              const children = [...(node.nodes || [])]
              for (const child of children) {
                if ('nodes' in child && child.nodes) flattenLayers(child)
              }
              node.replaceWith(...children)
              continue
            }
            if ('nodes' in node && node.nodes) flattenLayers(node)
          }
        }
        flattenLayers(root)
        asset.source = root.toString()
      }
    },
  }
}

export default defineConfig(({ mode }) => ({
  plugins: [
    react(),
    ...(mode === 'android' ? [androidCssCompatibility()] : []),
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
