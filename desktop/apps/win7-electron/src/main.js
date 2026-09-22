const { VelopackApp } = require('velopack')
let velopackRestarted = false
function removeBrandedShortcuts() {
  const path = require('node:path')
  const fs = require('node:fs')
  const profile = process.env.USERPROFILE
  if (!profile) return
  const locations = [
    path.join(profile, 'Desktop'),
    path.join(profile, 'AppData', 'Roaming', 'Microsoft', 'Windows', 'Start Menu', 'Programs'),
  ]
  for (const location of locations) {
    for (const name of ['滨湖智慧平台.lnk', 'BinhuDesktop.lnk']) {
      try { fs.unlinkSync(path.join(location, name)) } catch (_error) {}
    }
  }
}
VelopackApp.build()
  .setAutoApplyOnStartup(false)
  .onBeforeUninstallFastCallback(removeBrandedShortcuts)
  .onRestarted(() => { velopackRestarted = true })
  .run()

const path = require('node:path')
const fs = require('node:fs')
const { app, BrowserWindow, dialog, ipcMain, protocol, shell } = require('electron')
const http = require('node:http')
const https = require('node:https')
const { ElectronUpdateController } = require('./updater')
const { LocalMacService } = require('./local-mac-service')

const root = path.resolve(__dirname, '..', '..', '..')
const configPath = path.join(root, 'config', 'desktop.config.json')
const shellUi = path.join(root, 'apps', 'shell-ui')
const config = require(configPath)
const smokeTest = process.argv.includes('--smoke-test')
let updateController = null
let upgradeInfo = null
let localMacService = null

function probeResidenceUrl(baseUrl, pathName) {
  let base
  try { base = new URL(String(baseUrl || '').trim()) } catch (_error) { throw new Error('config_error') }
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash || base.pathname.includes('..')) {
    throw new Error('config_error')
  }
  const prefix = base.pathname.replace(/\/+$/, '')
  return new URL(`${prefix}${pathName.startsWith('/') ? pathName : `/${pathName}`}`, `${base.protocol}//${base.host}`)
}

function residenceRequest(url, { method = 'GET', body, headers = {}, timeoutSeconds }) {
  const transport = url.protocol === 'https:' ? https : http
  return new Promise((resolve, reject) => {
    const request = transport.request(url, {
      method,
      headers: {
        Accept: 'application/json',
        ...headers,
        ...(body ? { 'Content-Type': 'application/json;charset=UTF-8', 'Content-Length': Buffer.byteLength(body) } : {}),
        Connection: 'close',
      },
      timeout: Math.min(120, Math.max(1, Number(timeoutSeconds || 15))) * 1000,
      ...(url.protocol === 'https:' ? { rejectUnauthorized: false } : {}),
    }, response => {
      let text = ''
      let size = 0
      response.setEncoding('utf8')
      response.on('data', chunk => {
        size += Buffer.byteLength(chunk)
        if (size <= 1024 * 1024) text += chunk
      })
      response.on('end', () => {
        if (size > 1024 * 1024) return reject(new Error('response_too_large'))
        let payload = null
        try { payload = JSON.parse(text) } catch (_error) { return reject(new Error('invalid_response')) }
        resolve({ statusCode: response.statusCode || 0, payload })
      })
      response.on('error', () => reject(new Error('network_error')))
    })
    request.on('timeout', () => request.destroy(new Error('timeout')))
    request.on('error', error => reject(new Error(error?.message === 'timeout' ? 'timeout' : 'network_error')))
    if (body) request.write(body)
    request.end()
  })
}

const RESIDENCE_READ_PATHS = [
  { method: 'GET', pattern: /^\/sys\/randomImage\/[0-9]+$/ },
  { method: 'POST', pattern: /^\/sys\/login$/ },
  { method: 'POST', pattern: /^\/szjzz\/searchIsck$/ },
  { method: 'POST', pattern: /^\/szjzz\/searchzzrk$/ },
]
const RESIDENCE_HEADER_ALLOWLIST = new Set(['content-type', 'x-access-token', 'tenant_id', 'accept'])

function extractResidenceOrganizationCode(value) {
  const queue = [value]
  while (queue.length) {
    const current = queue.shift()
    if (!current || typeof current !== 'object') continue
    for (const key of ['orgCode', 'org_code', 'departCode']) {
      const candidate = String(current[key] || '').trim()
      if (candidate.length >= 6 && /^\d{6}/.test(candidate)) return candidate
    }
    queue.push(...(Array.isArray(current) ? current : Object.values(current)))
  }
  return ''
}

function residenceApiPath(baseUrl, pathName, method) {
  if (typeof pathName !== 'string' || !pathName.startsWith('/') || pathName.includes('..') || pathName.includes('?') || pathName.includes('#')) throw new Error('config_error')
  if (!RESIDENCE_READ_PATHS.some(rule => rule.method === method && rule.pattern.test(pathName))) throw new Error('config_error')
  return probeResidenceUrl(baseUrl, pathName)
}

function requestResidenceApi(request) {
  const method = request?.method === 'GET' ? 'GET' : request?.method === 'POST' ? 'POST' : ''
  if (!method) return Promise.reject(new Error('config_error'))
  const url = residenceApiPath(request.baseUrl, request.path, method)
  const headers = {}
  for (const [key, value] of Object.entries(request.headers || {})) {
    if (!RESIDENCE_HEADER_ALLOWLIST.has(String(key).toLowerCase()) || typeof value !== 'string' || value.length > 512) return Promise.reject(new Error('config_error'))
    headers[key] = value
  }
  const body = typeof request.body === 'string' ? request.body : undefined
  if (body && Buffer.byteLength(body) > 256 * 1024) return Promise.reject(new Error('config_error'))
  return residenceRequest(url, { method, body, headers, timeoutSeconds: request.timeoutSeconds })
}

function probeResidenceLogin(request) {
  const username = String(request?.username || '').trim()
  const password = String(request?.password || '')
  const mac = String(request?.mac || '').trim().toUpperCase()
  if (!username || !password || !/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(mac)) return { status: 'config_error', errorCode: 'missing_credentials' }
  let captchaUrl
  try { captchaUrl = probeResidenceUrl(request.baseUrl, `/sys/randomImage/${Date.now()}`) } catch (_error) { return { status: 'config_error', errorCode: 'invalid_base_url' } }
  const timeoutSeconds = Number(request.timeoutSeconds || 15)
  return residenceRequest(captchaUrl, { timeoutSeconds })
    .then(captcha => {
      if (captcha.statusCode < 200 || captcha.statusCode >= 300 || captcha.payload?.success !== true) return { status: 'rejected', errorCode: 'captcha_rejected' }
      const loginUrl = probeResidenceUrl(request.baseUrl, '/sys/login')
      return residenceRequest(loginUrl, {
        method: 'POST', timeoutSeconds,
        body: JSON.stringify({ username, password, mac, remember_me: true, captcha: '', checkKey: captchaUrl.pathname.split('/').pop(), terminalType: 1 }),
      }).then(login => {
        const result = login.payload?.result
        const token = result?.token
        if (login.statusCode < 200 || login.statusCode >= 300 || login.payload?.success !== true || !token) return { status: 'rejected', errorCode: 'login_rejected' }
        const organizationCode = extractResidenceOrganizationCode(result)
        const expected = String(request.communityCode || '').trim().toUpperCase()
        const actual = organizationCode.toUpperCase()
        if (expected && actual && !(expected === actual || (expected.length >= 6 && actual.startsWith(expected)) || (actual.length >= 6 && expected.startsWith(actual)))) return { status: 'rejected', errorCode: 'organization_mismatch' }
        return { status: 'allowed', organizationCode }
      })
    })
    .catch(error => ({ status: error?.message === 'config_error' ? 'config_error' : 'network_error', errorCode: error?.message || 'network_error' }))
}

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
ipcMain.handle('desktop:save-file', async (event, payload) => {
  const filename = typeof payload?.filename === 'string' ? payload.filename : '下载文件'
  const safeName = path.basename(filename).replace(/[\\/:*?"<>|]/g, '_') || '下载文件'
  const data = payload?.data
  if (!Array.isArray(data) && !Buffer.isBuffer(data) && !(data instanceof Uint8Array)) {
    throw new Error('下载文件内容无效')
  }
  const extension = path.extname(safeName).slice(1)
  const options = {
    title: '保存导出文件',
    defaultPath: path.join(app.getPath('downloads'), safeName),
    filters: extension ? [{ name: extension.toUpperCase(), extensions: [extension] }] : [],
  }
  const parent = BrowserWindow.fromWebContents(event.sender) || mainWindow()
  const result = parent
    ? await dialog.showSaveDialog(parent, options)
    : await dialog.showSaveDialog(options)
  if (result.canceled || !result.filePath) return false
  await fs.promises.writeFile(result.filePath, Buffer.from(data))
  return true
})
ipcMain.handle('desktop:get-update-status', () => updateController?.snapshot() || null)
ipcMain.handle('desktop:get-upgrade-info', () => upgradeInfo)
ipcMain.handle('desktop:acknowledge-upgrade', () => { acknowledgeUpgrade(); return upgradeInfo })
ipcMain.handle('desktop:check-for-updates', () => updateController?.checkForUpdates())
ipcMain.handle('desktop:download-update', () => updateController?.downloadUpdate())
ipcMain.handle('desktop:restart-and-apply', () => updateController?.restartAndApply())
ipcMain.handle('desktop:get-local-mac', () => localMacService?.getMac())
ipcMain.handle('desktop:set-local-mac', (_event, mac) => localMacService?.setMac(mac))
ipcMain.handle('desktop:probe-residence-login', async (_event, request) => probeResidenceLogin(request))
ipcMain.handle('desktop:request-residence-api', async (_event, request) => requestResidenceApi(request))

app.whenReady().then(async () => {
  protocol.handle('binhu', handleLocalAsset)
  localMacService = new LocalMacService(path.join(app.getPath('userData'), 'mac-address'))
  try {
    await localMacService.start()
  } catch (_error) {
    // The offline page reports a bounded port-occupancy error through IPC.
  }
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
app.on('before-quit', () => { void localMacService?.close() })
app.on('web-contents-created', (_event, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url)
    return { action: 'deny' }
  })
})
