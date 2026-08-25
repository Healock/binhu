const fs = require('node:fs')
const path = require('node:path')
const { HttpSource, UpdateManager } = require('velopack')

const UPDATE_URL = 'https://47.100.44.36/updates/win7-x64/'
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1_000
const POLICY_URL = `${UPDATE_URL}policy.stable.json`
const DELTA_DOWNLOAD_SHARE = 70
const DELTA_PROGRESS_POLL_MS = 500
const VELOPACK_RESTART_ARGUMENT = '--binhu-after-update'

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

function normalizeProgress(value) {
  let numeric = value
  if (value && typeof value === 'object') {
    numeric = value.percent ?? value.percentage ?? value.progress
  }
  numeric = Number(numeric)
  if (!Number.isFinite(numeric)) return null
  if (numeric > 0 && numeric < 1) numeric *= 100
  return Math.max(0, Math.min(100, Math.round(numeric)))
}

function partialPackagePath(packagesDirectory, fileName) {
  const extension = path.extname(fileName)
  const baseName = extension ? fileName.slice(0, -extension.length) : fileName
  return path.join(packagesDirectory, `${baseName}.partial`)
}

function deltaDownloadProgress(update, packagesDirectory) {
  const deltas = Array.isArray(update?.DeltasToTarget)
    ? update.DeltasToTarget.filter(item => item?.FileName && Number(item.Size) > 0)
    : []
  if (!packagesDirectory || deltas.length === 0) return null

  const totalBytes = deltas.reduce((sum, item) => sum + Number(item.Size), 0)
  if (totalBytes <= 0) return null
  let downloadedBytes = 0
  for (const item of deltas) {
    const completePath = path.join(packagesDirectory, item.FileName)
    const temporaryPath = partialPackagePath(packagesDirectory, item.FileName)
    let size = 0
    try {
      size = fs.statSync(completePath).size
    } catch (_error) {
      try {
        size = fs.statSync(temporaryPath).size
      } catch (_ignored) {}
    }
    downloadedBytes += Math.min(Number(item.Size), size)
  }
  return Math.min(DELTA_DOWNLOAD_SHARE - 1, Math.floor(downloadedBytes / totalBytes * DELTA_DOWNLOAD_SHARE))
}

class ElectronUpdateController {
  constructor({ currentVersion, enabled, emit, quit, beforeApply, packagesDirectory, logPath }) {
    this.enabled = enabled
    this.emit = emit
    this.quit = quit
    this.beforeApply = beforeApply || (() => {})
    this.packagesDirectory = packagesDirectory || null
    this.logPath = logPath || null
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

  log(event, details = {}) {
    if (!this.logPath) return
    try {
      fs.mkdirSync(path.dirname(this.logPath), { recursive: true })
      fs.appendFileSync(this.logPath, `${JSON.stringify({
        time: new Date().toISOString(),
        event,
        ...details,
      })}\n`, 'utf8')
    } catch (_error) {}
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
    this.log('check-start', { currentVersion: this.state.currentVersion })
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
        this.log('check-ready', { availableVersion: pending.Version })
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
        this.log('check-current')
        return this.setState({ state: 'idle', availableVersion: null, mandatory })
      }
      this.log('check-available', {
        availableVersion: update.TargetFullRelease.Version,
        fullBytes: update.TargetFullRelease.Size,
        deltaCount: Array.isArray(update.DeltasToTarget) ? update.DeltasToTarget.length : 0,
        deltaBytes: Array.isArray(update.DeltasToTarget)
          ? update.DeltasToTarget.reduce((sum, item) => sum + Number(item.Size || 0), 0)
          : 0,
      })
      return this.setState({
        state: 'available',
        availableVersion: update.TargetFullRelease.Version,
        mandatory,
      })
    } catch (error) {
      this.log('check-error', { message: errorMessage(error) })
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
      this.log('download-start', {
        availableVersion: this.state.availableVersion,
        packagesDirectory: this.packagesDirectory,
        deltaCount: Array.isArray(this.pendingUpdate.DeltasToTarget) ? this.pendingUpdate.DeltasToTarget.length : 0,
      })
      let lastProgress = 0
      const reportProgress = (value, source) => {
        const normalized = normalizeProgress(value)
        if (normalized == null || normalized <= lastProgress) return
        lastProgress = normalized
        this.setState({ state: 'downloading', progress: normalized })
        if (normalized === 100 || normalized % 10 === 0) {
          this.log('download-progress', { progress: normalized, source })
        }
      }
      const pollDeltaProgress = () => {
        // Velopack 1.2 does not forward network progress while downloading delta packages.
        const progress = deltaDownloadProgress(this.pendingUpdate, this.packagesDirectory)
        if (progress != null) reportProgress(progress, 'delta-file')
      }
      pollDeltaProgress()
      const progressTimer = setInterval(pollDeltaProgress, DELTA_PROGRESS_POLL_MS)
      progressTimer.unref?.()
      try {
        await this.getManager().downloadUpdateAsync(this.pendingUpdate, progress => {
          reportProgress(progress, 'velopack')
        })
        clearInterval(progressTimer)
        this.log('download-complete', { availableVersion: this.state.availableVersion })
        return this.setState({ state: 'ready', progress: 100 })
      } catch (error) {
        clearInterval(progressTimer)
        this.log('download-error', { message: errorMessage(error) })
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
      this.beforeApply(this.state.currentVersion, this.pendingUpdate.Version || this.state.availableVersion)
      // Velopack starts the launcher before its updater process has fully exited.
      // On Windows 7 that can leave the freshly replaced Electron executable
      // temporarily locked, so the native launcher waits for its parent only on
      // this update-restart path.
      this.getManager().waitExitThenApplyUpdate(
        this.pendingUpdate,
        false,
        true,
        [VELOPACK_RESTART_ARGUMENT],
      )
      this.quit()
    } catch (error) {
      return this.setState({ state: 'error', error: errorMessage(error) })
    }
    return this.snapshot()
  }

  schedule() {
    if (!this.enabled) return
    // 启动后立即检查一次；设置页只复用当前状态，不会额外触发启动检查。
    const first = setTimeout(() => { void this.checkForUpdates() }, 0)
    const recurring = setInterval(() => { void this.checkForUpdates() }, CHECK_INTERVAL_MS)
    first.unref()
    recurring.unref()
  }
}

module.exports = {
  ElectronUpdateController,
  VELOPACK_RESTART_ARGUMENT,
  deltaDownloadProgress,
  normalizeProgress,
  partialPackagePath,
}
