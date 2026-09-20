const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')

const DEFAULT_MAC = '02:00:00:00:00:01'

function normalizeMacAddress(value) {
  const compact = String(value || '').trim().replace(/[.\-:\s]/g, '').toUpperCase()
  if (!/^[0-9A-F]{12}$/.test(compact)) throw new Error('MAC 地址必须是 12 位十六进制字符')
  const normalized = compact.match(/.{2}/g).join(':')
  if (normalized === '00:00:00:00:00:00' || (Number.parseInt(compact.slice(0, 2), 16) & 1) === 1) {
    throw new Error('MAC 地址必须是有效的单播地址')
  }
  return normalized
}

function detectHardwareMac(interfaces = os.networkInterfaces()) {
  const candidates = []
  for (const [name, addresses] of Object.entries(interfaces || {})) {
    for (const address of addresses || []) {
      if (address?.internal || !address?.mac) continue
      try {
        const mac = normalizeMacAddress(address.mac)
        const virtual = /virtual|vethernet|loopback|tunnel|docker|vmware|hyper-v/i.test(name)
        candidates.push({ mac, virtual, name })
      } catch (_error) {}
    }
  }
  candidates.sort((left, right) => Number(left.virtual) - Number(right.virtual) || left.name.localeCompare(right.name))
  return candidates[0]?.mac || DEFAULT_MAC
}

class LocalMacService {
  constructor(statePath, port = 23333, hardwareMac = detectHardwareMac()) {
    this.statePath = statePath
    this.port = port
    this.server = null
    try {
      this.mac = normalizeMacAddress(fs.readFileSync(statePath, 'utf8'))
    } catch (_error) {
      try { this.mac = normalizeMacAddress(hardwareMac) } catch (_ignored) { this.mac = DEFAULT_MAC }
      this.persistMac(this.mac)
    }
  }

  persistMac(mac) {
    fs.mkdirSync(path.dirname(this.statePath), { recursive: true })
    const temporary = `${this.statePath}.partial`
    fs.writeFileSync(temporary, `${mac}\n`, { encoding: 'ascii', mode: 0o600 })
    try { fs.unlinkSync(this.statePath) } catch (error) { if (error.code !== 'ENOENT') throw error }
    fs.renameSync(temporary, this.statePath)
  }

  start() {
    if (this.server) return Promise.resolve()
    const server = http.createServer((request, response) => {
      response.setHeader('Access-Control-Allow-Origin', '*')
      response.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS')
      response.setHeader('Cache-Control', 'no-store')
      if (request.method === 'OPTIONS' && ['/', '/health'].includes(request.url)) {
        response.writeHead(204).end()
      } else if (request.method === 'GET' && request.url === '/') {
        response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' })
        response.end(JSON.stringify({ mac: this.mac }))
      } else if (request.method === 'GET' && request.url === '/health') {
        response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' })
        response.end('{"status":"ok"}')
      } else if (['/', '/health'].includes(request.url)) {
        response.writeHead(405, { 'Content-Type': 'application/json; charset=utf-8' })
        response.end('{"error":"method_not_allowed"}')
      } else {
        response.writeHead(404, { 'Content-Type': 'application/json; charset=utf-8' })
        response.end('{"error":"not_found"}')
      }
    })
    return new Promise((resolve, reject) => {
      server.once('error', reject)
      server.listen(this.port, '127.0.0.1', () => {
        server.removeListener('error', reject)
        this.server = server
        resolve()
      })
    })
  }

  getMac() {
    if (!this.server) throw new Error('本机 23333 端口未能启动，请检查端口占用')
    return this.mac
  }

  setMac(value) {
    const normalized = normalizeMacAddress(value)
    this.persistMac(normalized)
    this.mac = normalized
    return normalized
  }

  close() {
    if (!this.server) return Promise.resolve()
    const server = this.server
    this.server = null
    return new Promise(resolve => server.close(resolve))
  }
}

module.exports = { DEFAULT_MAC, LocalMacService, detectHardwareMac, normalizeMacAddress }
