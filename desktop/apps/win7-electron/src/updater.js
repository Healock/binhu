const { HttpSource, UpdateManager } = require('velopack')

const UPDATE_URL = 'https://47.100.44.36/updates/win7-x64/'
const INITIAL_CHECK_DELAY_MS = 15_000
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1_000
const POLICY_URL = `${UPDATE_URL}policy.stable.json`

function compareVersions(left, right) {
  const a = String(left).split('.').map(Number)
  const b = String(right).split('.').map(Number)
  if (a.length !== 3 || b.length !== 3 || [...a, ...b].some(Number.isNaN)) return 0
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1
  }
  return 0
}

function errorMessage(error) {
  if (error instanceof Error && error.message) return error.message
  return String(error || '未知更新错误')
}

class ElectronUpdateController {
  constructor({ currentVersion, enabled, emit, quit }) {
    this.enabled = enabled
    this.emit = emit
    this.quit = quit
    this.manager = null
    this.pendingUpdate = null
    this.running = null
    this.state = {
      state: 'idle',
      currentVersion,
      availableVersion: null,
      progress: null,
      mandatory: false,
      error: null,
    }
  }

  snapshot() {
    return { ...this.state }
  }

  setState(next) {
    this.state = { ...this.state, ...next }
    this.emit(this.snapshot())
    return this.snapshot()
  }

  getManager() {
    if (!this.enabled) {
      throw new Error('当前程序未通过 Velopack 安装，开发模式下不能检查更新。')
    }
    if (!this.manager) {
      this.manager = new UpdateManager(new HttpSource(UPDATE_URL, {
        TimeoutMilliseconds: 30_000,
      }), {
        AllowVersionDowngrade: false,
        ExplicitChannel: 'stable',
        MaximumDeltasBeforeFallback: 10,
      })
    }
    return this.manager
  }

  async runExclusive(operation) {
    if (this.running) return this.running
    this.running = operation().finally(() => { this.running = null })
    return this.running
  }

  async performCheck() {
    this.setState({ state: 'checking', progress: null, error: null })
    let mandatory = false
    try {
      try {
        const response = await fetch(POLICY_URL, { signal: AbortSignal.timeout(10_000), cache: 'no-store' })
        if (response.ok) {
          const policy = await response.json()
          mandatory = compareVersions(this.state.currentVersion, policy.minimumVersion) < 0
        }
      } catch (_error) {
        // Policy lookup is advisory; the normal update feed remains usable.
      }
      const manager = this.getManager()
      const pending = manager.getUpdatePendingRestart()
      if (pending) {
        this.pendingUpdate = pending
        return this.setState({
          state: 'ready',
          availableVersion: pending.Version,
          progress: 100,
          mandatory,
        })
      }
      const update = await manager.checkForUpdatesAsync()
      this.pendingUpdate = update
      if (!update) {
        return this.setState({ state: 'idle', availableVersion: null, mandatory })
      }
      return this.setState({
        state: 'available',
        availableVersion: update.TargetFullRelease.Version,
        mandatory,
      })
    } catch (error) {
      return this.setState({ state: 'error', mandatory, error: errorMessage(error) })
    }
  }

  async checkForUpdates() {
    return this.runExclusive(() => this.performCheck())
  }

  async downloadUpdate() {
    return this.runExclusive(async () => {
      if (!this.pendingUpdate) {
        await this.performCheck()
      }
      if (!this.pendingUpdate) return this.snapshot()
      if (this.state.state === 'ready') return this.snapshot()
      this.setState({ state: 'downloading', progress: 0, error: null })
      try {
        await this.getManager().downloadUpdateAsync(this.pendingUpdate, progress => {
          this.setState({ state: 'downloading', progress })
        })
        return this.setState({ state: 'ready', progress: 100 })
      } catch (error) {
        return this.setState({ state: 'error', error: errorMessage(error) })
      }
    })
  }

  async restartAndApply() {
    if (!this.pendingUpdate || this.state.state !== 'ready') {
      return this.setState({ state: 'error', error: '更新尚未下载完成。' })
    }
    try {
      this.setState({ state: 'applying', error: null })
      this.getManager().waitExitThenApplyUpdate(this.pendingUpdate, false, true)
      this.quit()
    } catch (error) {
      return this.setState({ state: 'error', error: errorMessage(error) })
    }
    return this.snapshot()
  }

  schedule() {
    if (!this.enabled) return
    const first = setTimeout(() => { void this.checkForUpdates() }, INITIAL_CHECK_DELAY_MS)
    const recurring = setInterval(() => { void this.checkForUpdates() }, CHECK_INTERVAL_MS)
    first.unref()
    recurring.unref()
  }
}

module.exports = { ElectronUpdateController }
