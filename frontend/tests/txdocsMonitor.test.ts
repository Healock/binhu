import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const apiSource = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
const pageSource = readFileSync(new URL('../src/pages/TxDocsMonitorSettings.tsx', import.meta.url), 'utf8')

test('腾讯只读连接凭据使用独立保存接口', () => {
  assert.match(apiSource, /put\('\/stats\/txdocs-monitor\/config\/credentials', payload\)/)
  assert.match(pageSource, /保存连接凭据/)
  assert.match(pageSource, /updateTxDocsMonitorCredentials\(\{/)
  assert.match(pageSource, /!clientId\.trim\(\).* !accessToken\.trim\(\).* !openId\.trim\(\)/s)
  assert.match(pageSource, /setClientId\(''\).*setAccessToken\(''\).*setOpenId\(''\)/s)
})

test('保存监控目标不会携带尚未确认的连接凭据', () => {
  const saveTargetStart = pageSource.indexOf('const saveTarget = async')
  const saveCredentialsStart = pageSource.indexOf('const saveCredentials = async')
  assert.ok(saveTargetStart >= 0 && saveCredentialsStart > saveTargetStart)
  const saveTargetSource = pageSource.slice(saveTargetStart, saveCredentialsStart)
  assert.doesNotMatch(saveTargetSource, /client_id|access_token|open_id/)
  assert.match(pageSource, /保存目标<\/Button>/)
  assert.match(pageSource, /disabled=\{savingCredentials\}.*保存目标/s)
})

test('连接凭据说明要求三个字段来自同一次授权', () => {
  assert.match(pageSource, /三个字段必须来自同一次腾讯授权/)
  assert.match(pageSource, /保存后不会回显/)
})

test('外部监控配置允许疑似未注销模型三', () => {
  assert.match(pageSource, /疑似未注销模型三/)
})
