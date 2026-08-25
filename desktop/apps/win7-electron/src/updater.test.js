const assert = require('node:assert/strict')
const test = require('node:test')

const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  ElectronUpdateController,
  VELOPACK_RESTART_ARGUMENT,
  deltaDownloadProgress,
  normalizeProgress,
  partialPackagePath,
} = require('./updater')

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

test('normalizes supported progress callback values', () => {
  assert.equal(normalizeProgress(45), 45)
  assert.equal(normalizeProgress(0.45), 45)
  assert.equal(normalizeProgress({ percent: 45 }), 45)
  assert.equal(normalizeProgress({ percentage: 140 }), 100)
  assert.equal(normalizeProgress({ progress: -1 }), 0)
  assert.equal(normalizeProgress('invalid'), null)
})

test('estimates delta download progress from Velopack partial packages', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'binhu-update-progress-'))
  try {
    const update = {
      DeltasToTarget: [
        { FileName: 'app-0.25.16-delta.nupkg', Size: 1000 },
        { FileName: 'app-0.25.17-delta.nupkg', Size: 1000 },
      ],
    }
    fs.writeFileSync(partialPackagePath(directory, update.DeltasToTarget[0].FileName), Buffer.alloc(500))
    assert.equal(deltaDownloadProgress(update, directory), 17)
    fs.writeFileSync(path.join(directory, update.DeltasToTarget[0].FileName), Buffer.alloc(1000))
    assert.equal(deltaDownloadProgress(update, directory), 35)
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

test('download progress stays monotonic when Velopack reports zero after file polling', async (context) => {
  context.mock.method(global, 'fetch', async () => ({ ok: false }))
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'binhu-update-progress-'))
  const update = {
    TargetFullRelease: { Version: '0.25.16' },
    DeltasToTarget: [{ FileName: 'app-0.25.16-delta.nupkg', Size: 1000 }],
  }
  const states = []
  const controller = new ElectronUpdateController({
    currentVersion: '0.25.15',
    enabled: true,
    emit: state => states.push(state),
    quit: () => {},
    packagesDirectory: directory,
  })
  controller.manager = {
    getUpdatePendingRestart: () => null,
    checkForUpdatesAsync: async () => update,
    downloadUpdateAsync: async (_selected, progress) => {
      fs.writeFileSync(partialPackagePath(directory, update.DeltasToTarget[0].FileName), Buffer.alloc(500))
      await new Promise(resolve => setTimeout(resolve, 600))
      progress(0)
      progress(70)
    },
  }

  try {
    const result = await controller.downloadUpdate()
    const reported = states.filter(state => state.state === 'downloading').map(state => state.progress)
    assert.ok(reported.includes(35))
    assert.deepEqual(reported, [...reported].sort((left, right) => left - right))
    assert.equal(result.progress, 100)
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
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

test('restartAndApply marks the Win7 Velopack restart for the native launcher', async () => {
  const update = { Version: '0.25.16' }
  const calls = []
  let quitCalled = false
  const { controller } = makeController({
    waitExitThenApplyUpdate: (...args) => calls.push(args),
  })
  controller.pendingUpdate = update
  controller.state = { ...controller.state, state: 'ready', availableVersion: update.Version }
  controller.quit = () => { quitCalled = true }

  const result = await controller.restartAndApply()

  assert.deepEqual(calls, [[update, false, true, [VELOPACK_RESTART_ARGUMENT]]])
  assert.equal(quitCalled, true)
  assert.equal(result.state, 'applying')
})

test('schedule checks immediately on startup and keeps the periodic check', () => {
  const originalSetTimeout = global.setTimeout
  const originalSetInterval = global.setInterval
  const delays = []
  let checks = 0
  try {
    global.setTimeout = (callback, delay) => {
      delays.push(['timeout', delay])
      callback()
      return { unref() {} }
    }
    global.setInterval = (_callback, delay) => {
      delays.push(['interval', delay])
      return { unref() {} }
    }
    const { controller } = makeController({})
    controller.checkForUpdates = async () => { checks += 1; return controller.snapshot() }
    controller.schedule()
    assert.equal(checks, 1)
    assert.deepEqual(delays, [
      ['timeout', 0],
      ['interval', 6 * 60 * 60 * 1_000],
    ])
  } finally {
    global.setTimeout = originalSetTimeout
    global.setInterval = originalSetInterval
  }
})
