import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveConfig } from 'vite'

test('environment artifact uses relative assets and its own output directory', async () => {
  const config = await resolveConfig({ mode: 'environment' }, 'build')
  assert.equal(config.base, './')
  assert.equal(config.build.outDir, 'dist-environment')
})

test('production and client artifacts preserve their existing root base', async () => {
  for (const mode of ['production', 'desktop', 'android']) {
    const config = await resolveConfig({ mode }, 'build')
    assert.equal(config.base, '/')
    assert.equal(config.build.outDir, 'dist')
  }
})
