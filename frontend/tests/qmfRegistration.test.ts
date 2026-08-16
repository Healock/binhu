import assert from 'node:assert/strict'
import test from 'node:test'

import {
  canExecutePreparedQmfRun,
  qmfRunIsPolling,
} from '../src/utils/qmfRegistration.ts'
import type { QmfRegistrationRun } from '../src/api/client.ts'

function run(status: QmfRegistrationRun['status']): QmfRegistrationRun {
  return {
    id: 1,
    parser_type: '疑似未注销模型三',
    source_id: 2,
    expected_revision: 3,
    status,
    steps: [],
    result_code: '',
    photo: { sha256: '', mime_type: '', size_bytes: 0 },
    tencent_marker_status: 'not_started',
    tencent_marker_error: '',
    prepared_at: null,
    expires_at: null,
    execution_started_at: null,
    completed_at: null,
    created_at: null,
    updated_at: null,
    can_execute: status === 'prepared',
    can_reprepare: status === 'failed',
    can_retry_marker: false,
  }
}

test('真实登记必须同时具备新鲜预演和准备状态', () => {
  assert.equal(canExecutePreparedQmfRun(run('prepared'), true), true)
  assert.equal(canExecutePreparedQmfRun(run('prepared'), false), false)
  assert.equal(canExecutePreparedQmfRun(run('failed'), true), false)
})

test('只有执行中状态需要轮询', () => {
  assert.equal(qmfRunIsPolling(run('executing')), true)
  assert.equal(qmfRunIsPolling(run('succeeded')), false)
  const markerWriting = run('succeeded')
  markerWriting.tencent_marker_status = 'writing'
  assert.equal(qmfRunIsPolling(markerWriting), true)
  assert.equal(qmfRunIsPolling(null), false)
})
