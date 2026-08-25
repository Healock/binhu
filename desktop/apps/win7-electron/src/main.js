const { VelopackApp } = require('velopack')
let velopackRestarted = false
VelopackApp.build()
  .setAutoApplyOnStartup(false)
  .onRestarted(() => { velopackRestarted = true })
  .run()

const path = require('node:path')
const fs = require('node:fs')
const { app, BrowserWindow, ipcMain, protocol, shell } = require('electron')
const { ElectronUpdateController } = require('./updater')

const root = path.resolve(__dirname, '..', '..', '..')
const configPath = path.join(root, 'config', 'desktop.config.json')
const shellUi = path.join(root, 'apps', 'shell-ui')
const config = require(configPath)
const smokeTest = process.argv.includes('--smoke-test')
let updateController = null
let upgradeInfo = null

function upgradeStatePath() {
  return path.join(app.getPath('userData'), 'upgrade-state.json')
}

function loadUpgradeInfo() {
  const currentVersion = config.appVersion
  let state = {}
  try {
    state = JSON.parse(fs.readFileSync(upgradeStatePath(), 'utf8'))
  } catch (_error) {
    state = {}
  }
  const previousVersion = typeof state.lastStartedVersion === 'string' ? state.lastStartedVersion : null
  const pendingFrom = typeof state.pendingFrom === 'string' ? state.pendingFrom : null
  const restartedMarker = velopackRestarted || pendingFrom === '__velopack_restarted__'
  const upgradedFrom = pendingFrom && pendingFrom !== currentVersion
    && pendingFrom !== '__velopack_restarted__'
    ? pendingFrom
    : (!pendingFrom && previousVersion && previousVersion !== currentVersion ? previousVersion : null)
  upgradeInfo = { currentVersion, upgradedFrom, upgradeDetected: Boolean(upgradedFrom || restartedMarker) }
  writeUpgradeState({ lastStartedVersion: currentVersion, pendingFrom: upgradedFrom || (restartedMarker ? '__velopack_restarted__' : null) })
}

function writeUpgradeState(state) {
  const destination = upgradeStatePath()
  const temporary = `${destination}.partial`
  try {
    fs.mkdirSync(path.dirname(destination), { recursive: true })
    fs.writeFileSync(temporary, JSON.stringify(state), 'utf8')
    fs.renameSync(temporary, destination)
  } catch (_error) {
    try { fs.unlinkSync(temporary) } catch (_ignored) {}
  }
}

function acknowledgeUpgrade() {
  if (!upgradeInfo) return
  upgradeInfo = { ...upgradeInfo, upgradedFrom: null, upgradeDetected: false }
  writeUpgradeState({ lastStartedVersion: config.appVersion, pendingFrom: null })
}

// Windows 7 has no DirectComposition implementation. Keep ANGLE/GPU rendering
// enabled while preventing Chromium from probing unsupported DComp interfaces.
app.commandLine.appendSwitch('disable-direct-composition')

protocol.registerSchemesAsPrivileged([{
  scheme: 'binhu',
  privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: false },
}])

function contentType(filePath) {
  const extension = path.extname(filePath).toLowerCase()
  return ({
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8', '.png': 'image/png',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
    '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.woff': 'font/woff',
    '.woff2': 'font/woff2',
  })[extension] || 'application/octet-stream'
}

async function handleLocalAsset(request) {
  const requestUrl = new URL(request.url)
  const rootPath = path.resolve(shellUi)
  const requestedPath = decodeURIComponent(requestUrl.pathname).replace(/^\/+/, '')
  let relativePath = requestedPath || 'index.html'
  let filePath = path.resolve(rootPath, relativePath)
  if (!filePath.startsWith(`${rootPath}${path.sep}`) && filePath !== rootPath) {
    return new Response('Forbidden', { status: 403 })
  }
  try {
    await fs.promises.access(filePath, fs.constants.R_OK)
  } catch (error) {
    if (error.code !== 'ENOENT' || path.extname(relativePath)) {
      return new Response(error.code === 'ENOENT' ? 'Not found' : 'Unable to read asset', {
        status: error.code === 'ENOENT' ? 404 : 500,
      })
    }
    relativePath = 'index.html'
    filePath = path.join(rootPath, relativePath)
  }
  try {
    const body = await fs.promises.readFile(filePath)
    return new Response(body, { headers: { 'content-type': contentType(filePath) } })
  } catch (_error) {
    return new Response('Unable to read asset', { status: 500 })
  }
}

function createMainWindow() {
  const window = new BrowserWindow({
    width: 1440, height: 960, minWidth: 1024, minHeight: 640, show: false,
    frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: true,
    title: config.appName,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'), contextIsolation: true,
      nodeIntegration: false, sandbox: true,
    },
  })
  window.once('ready-to-show', () => window.show())
  window.loadURL('binhu://app/login')
  if (smokeTest) {
    window.webContents.once('did-finish-load', async () => {
      const ready = await window.webContents.executeJavaScript(
        "Boolean(document.querySelector('input[aria-label=\\\"用户名\\\"]') && document.getElementById('offline-mode-button') && document.getElementById('window-close-button'))",
      )
      const image = await window.webContents.capturePage()
      const screenshotPath = path.join(root, '..', '.temp', 'electron-local-login.png')
      await fs.promises.mkdir(path.dirname(screenshotPath), { recursive: true })
      await fs.promises.writeFile(screenshotPath, image.toPNG())
      console.log(`Electron local frontend smoke test: ${ready ? 'OK' : 'FAILED'}`)
      app.exit(ready ? 0 : 1)
    })
  }
  return window
}

function mainWindow() { return BrowserWindow.getAllWindows()[0] || null }

function velopackPackagesDirectory() {
  const executableDirectory = path.dirname(process.execPath)
  const installRoot = path.dirname(executableDirectory)
  return path.join(installRoot, 'packages')
}

ipcMain.handle('desktop:get-config', () => ({
  schemaVersion: config.schemaVersion, appName: config.appName, appVersion: config.appVersion,
  serverUrl: config.serverUrl, apiBaseUrl: config.apiBaseUrl, initialRoute: config.initialRoute,
}))
ipcMain.handle('desktop:open-online', async () => {
  const window = mainWindow()
  if (window) { await window.loadURL('binhu://app/login'); window.show(); window.focus() }
})
ipcMain.handle('desktop:open-offline', async () => {
  const window = mainWindow()
  if (window) { await window.loadURL('binhu://app/offline'); window.show(); window.focus() }
})
ipcMain.handle('desktop:window-minimize', (event) => {
  BrowserWindow.fromWebContents(event.sender)?.minimize()
})
ipcMain.handle('desktop:window-toggle-maximize', (event) => {
  const window = BrowserWindow.fromWebContents(event.sender)
  if (!window) return false
  if (window.isMaximized()) window.unmaximize()
  else window.maximize()
  return window.isMaximized()
})
ipcMain.handle('desktop:window-is-maximized', (event) => (
  BrowserWindow.fromWebContents(event.sender)?.isMaximized() || false
))
ipcMain.handle('desktop:window-close', (event) => {
  BrowserWindow.fromWebContents(event.sender)?.close()
})
ipcMain.handle('desktop:save-file', async (_event, payload) => {
  const filename = typeof payload?.filename === 'string' ? payload.filename : '下载文件'
  const safeName = path.basename(filename).replace(/[\\/:*?"<>|]/g, '_') || '下载文件'
  const data = payload?.data
  if (!Array.isArray(data) && !Buffer.isBuffer(data) && !(data instanceof Uint8Array)) {
    throw new Error('下载文件内容无效')
  }
  const target = path.join(app.getPath('downloads'), safeName)
  await fs.promises.writeFile(target, Buffer.from(data))
  return target
})
ipcMain.handle('desktop:get-update-status', () => updateController?.snapshot() || null)
ipcMain.handle('desktop:get-upgrade-info', () => upgradeInfo)
ipcMain.handle('desktop:acknowledge-upgrade', () => { acknowledgeUpgrade(); return upgradeInfo })
ipcMain.handle('desktop:check-for-updates', () => updateController?.checkForUpdates())
ipcMain.handle('desktop:download-update', () => updateController?.downloadUpdate())
ipcMain.handle('desktop:restart-and-apply', () => updateController?.restartAndApply())

app.whenReady().then(() => {
  protocol.handle('binhu', handleLocalAsset)
  loadUpgradeInfo()
  updateController = new ElectronUpdateController({
    currentVersion: config.appVersion,
    enabled: app.isPackaged && !smokeTest,
    emit: state => {
      for (const window of BrowserWindow.getAllWindows()) {
        window.webContents.send('desktop:update-state', state)
      }
    },
    beforeApply: (fromVersion) => writeUpgradeState({
      lastStartedVersion: config.appVersion,
      pendingFrom: fromVersion,
    }),
    packagesDirectory: velopackPackagesDirectory(),
    logPath: path.join(app.getPath('userData'), 'logs', 'updater.log'),
    quit: () => app.quit(),
  })
  createMainWindow()
  updateController.schedule()
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createMainWindow() })
})
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
app.on('web-contents-created', (_event, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url)
    return { action: 'deny' }
  })
})
