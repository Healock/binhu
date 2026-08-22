const assert = require('node:assert/strict')
const test = require('node:test')

const { ElectronUpdateController } = require('./updater')

function makeController(manager) {
  const states = []
  const controller = new ElectronUpdateController({
    currentVersion: '0.25.15',
    enabled: true,
    emit: state => states.push(state),
    quit: () => {},
  })
  controller.manager = manager
  return { controller, states }
}

test('downloadUpdate checks and downloads in one call', async (context) => {
  context.mock.method(global, 'fetch', async () => ({ ok: false }))
  const update = { TargetFullRelease: { Version: '0.25.16' } }
  let downloads = 0
  const { controller } = makeController({
    getUpdatePendingRestart: () => null,
    checkForUpdatesAsync: async () => update,
    downloadUpdateAsync: async (selected, progress) => {
      assert.equal(selected, update)
      progress(45)
      downloads += 1
    },
  })

  const result = await Promise.race([
    controller.downloadUpdate(),
    new Promise((_, reject) => setTimeout(() => reject(new Error('download timed out')), 500)),
  ])

  assert.equal(downloads, 1)
  assert.equal(result.state, 'ready')
  assert.equal(result.availableVersion, '0.25.16')
  assert.equal(result.progress, 100)
})

test('downloadUpdate leaves an already downloaded update ready', async (context) => {
  context.mock.method(global, 'fetch', async () => ({ ok: false }))
  const downloaded = { Version: '0.25.16' }
  let downloads = 0
  const { controller } = makeController({
    getUpdatePendingRestart: () => downloaded,
    checkForUpdatesAsync: async () => null,
    downloadUpdateAsync: async () => { downloads += 1 },
  })

  const result = await controller.downloadUpdate()

  assert.equal(downloads, 0)
  assert.equal(result.state, 'ready')
  assert.equal(result.availableVersion, '0.25.16')
})
