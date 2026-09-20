const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')
const { LocalMacService, detectHardwareMac, normalizeMacAddress } = require('./local-mac-service')

test('normalizes supported unicast MAC formats', () => {
  assert.equal(normalizeMacAddress('02-11-22-33-44-66'), '02:11:22:33:44:66')
  assert.throws(() => normalizeMacAddress('01:11:22:33:44:66'), /单播/)
  assert.throws(() => normalizeMacAddress('00:00:00:00:00:00'), /单播/)
})

test('prefers a physical hardware MAC and falls back safely', () => {
  assert.equal(detectHardwareMac({
    'vEthernet (Default Switch)': [{ internal: false, mac: '02:00:00:00:00:09' }],
    Ethernet: [{ internal: false, mac: '02:11:22:33:44:66' }],
  }), '02:11:22:33:44:66')
  assert.equal(detectHardwareMac({ Loopback: [{ internal: true, mac: '00:00:00:00:00:00' }] }), '02:00:00:00:00:01')
})

test('serves and persists the desktop MAC on loopback only', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'binhu-mac-'))
  const statePath = path.join(directory, 'mac-address')
  const service = new LocalMacService(statePath, 0, '02:AA:BB:CC:DD:EE')
  await service.start()
  const address = service.server.address()
  const base = `http://127.0.0.1:${address.port}`
  try {
    assert.equal((await (await fetch(base)).json()).mac, '02:AA:BB:CC:DD:EE')
    assert.equal(fs.readFileSync(statePath, 'ascii').trim(), '02:AA:BB:CC:DD:EE')
    assert.equal(service.setMac('02.11.22.33.44.66'), '02:11:22:33:44:66')
    assert.equal((await (await fetch(base)).json()).mac, '02:11:22:33:44:66')
    assert.equal((await fetch(base, { method: 'POST' })).status, 405)
    assert.equal(fs.readFileSync(statePath, 'ascii').trim(), '02:11:22:33:44:66')
  } finally {
    await service.close()
    fs.rmSync(directory, { recursive: true, force: true })
  }
})
