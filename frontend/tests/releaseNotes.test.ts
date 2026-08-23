import assert from 'node:assert/strict'
import test from 'node:test'
import {
  isReleaseNotes,
  loadReleaseNotes,
  releaseNotesCandidates,
} from '../src/utils/releaseNotes.ts'

const validNotes = {
  schemaVersion: 1,
  version: '0.25.20',
  previousVersion: '0.25.19',
  pullRequests: [{ number: 343, title: '修复更新日志读取', summary: '修复桌面端本地资源读取。' }],
}

const sectionedNotes = {
  schemaVersion: 2,
  version: '0.25.21',
  previousVersion: '0.25.20',
  pullRequests: validNotes.pullRequests,
  sections: [{
    title: '任务编辑与照片回写可靠性',
    items: ['修复批量编辑。'],
    pullRequests: [343],
  }],
}

test('release notes use the current local protocol before generic fallbacks', () => {
  assert.deepEqual(releaseNotesCandidates('binhu://app/login').slice(0, 2), [
    'binhu://app/release-notes.json',
    'release-notes.json',
  ])
})

test('release notes load from a desktop-local URL', async () => {
  const requests: string[] = []
  const notes = await loadReleaseNotes(
    '0.25.20',
    async input => {
      requests.push(String(input))
      return new Response(JSON.stringify(validNotes), { status: 200 })
    },
    'http://tauri.localhost/login',
  )

  assert.deepEqual(notes, validNotes)
  assert.deepEqual(requests, ['http://tauri.localhost/release-notes.json'])
})

test('release notes try another URL form after a missing asset', async () => {
  const requests: string[] = []
  const notes = await loadReleaseNotes(
    '0.25.20',
    async input => {
      requests.push(String(input))
      if (requests.length < 2) return new Response('missing', { status: 404 })
      return new Response(JSON.stringify(validNotes), { status: 200 })
    },
    'binhu://app/login',
  )

  assert.deepEqual(notes, validNotes)
  assert.equal(requests.length, 2)
})

test('invalid or stale release notes are ignored', async () => {
  assert.equal(isReleaseNotes({ ...validNotes, pullRequests: [{ number: '343' }] }), false)
  const notes = await loadReleaseNotes(
    '0.25.20',
    async () => new Response(JSON.stringify({ ...validNotes, version: '0.25.19' }), { status: 200 }),
    'https://example.test/login',
  )
  assert.equal(notes, null)
})

test('sectioned release notes are accepted for curated user-facing changelogs', () => {
  assert.equal(isReleaseNotes(sectionedNotes), true)
  assert.equal(isReleaseNotes({ ...sectionedNotes, sections: [{ title: '坏数据', items: [1] }] }), false)
})
