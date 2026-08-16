import assert from 'node:assert/strict'
import test from 'node:test'

import {
  canExecutePreparedQmfRun,
  qmfRunCanReprepare,
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

test('纯写入前失败即使缺少后端派生标记也显示重新准备', () => {
  const legacy = run('failed')
  legacy.can_reprepare = false
  legacy.steps = [
    { key: 'query_task', label: '查询模型三任务', status: 'succeeded', result_code: '', started_at: null, finished_at: null },
    { key: 'query_person', label: '查询人员登记资料', status: 'succeeded', result_code: '', started_at: null, finished_at: null },
    { key: 'query_photo', label: '读取居住证照片', status: 'succeeded', result_code: '', started_at: null, finished_at: null },
    { key: 'precheck', label: '执行登记前校验', status: 'succeeded', result_code: '', started_at: null, finished_at: null },
    { key: 'upload_photo', label: '上传照片数据', status: 'pending', result_code: '', started_at: null, finished_at: null },
    { key: 'save_local_photo', label: '保存居住证照片关联', status: 'pending', result_code: '', started_at: null, finished_at: null },
    { key: 'register_person', label: '保存人员登记', status: 'pending', result_code: '', started_at: null, finished_at: null },
    { key: 'complete_task', label: '反馈模型三核查结果', status: 'pending', result_code: '', started_at: null, finished_at: null },
    { key: 'verify_final', label: '复核模型三最终状态', status: 'pending', result_code: '', started_at: null, finished_at: null },
  ]
  assert.equal(qmfRunCanReprepare(legacy), true)

  legacy.steps[4].status = 'sending'
  assert.equal(qmfRunCanReprepare(legacy), false)
})
