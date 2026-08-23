import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

function read(path: string) {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

test('version updates and important announcements share one fullscreen dialog component', () => {
  const gate = read('../src/components/VersionUpdatedGate.tsx')
  const dialog = read('../src/components/FullscreenNoticeDialog.tsx')

  assert.match(gate, /import FullscreenNoticeDialog/)
  assert.equal((gate.match(/<FullscreenNoticeDialog/g) || []).length, 2)
  assert.match(gate, /getImportantUnreadAnnouncements/)
  assert.match(gate, /announcement\.severity|important-announcement|重要公告/)
  assert.match(dialog, /className="fullscreen-notice"/)
  assert.match(dialog, /aria-modal="true"/)
})

test('important announcements are acknowledged on the server before leaving the queue', () => {
  const gate = read('../src/components/VersionUpdatedGate.tsx')

  assert.match(gate, /await markNotificationRead\(announcement\)/)
  assert.match(gate, /setAnnouncements\(current => current\.filter/)
  assert.match(gate, /阅读状态保存失败/)
})
