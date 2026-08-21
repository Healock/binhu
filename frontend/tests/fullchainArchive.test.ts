import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { apiErrorMessage } from '../src/api/client.ts'
import { canManageFullchainArchive } from '../src/utils/mobileTaskRouting.ts'

test('全链条归档错误展示兼容 FastAPI 校验数组且不渲染对象', () => {
  assert.equal(apiErrorMessage({ response: { data: { detail: '任务已变化' } } }, '失败'), '任务已变化')
  assert.equal(apiErrorMessage({ response: { data: { detail: [{ msg: '字段错误' }] } } }, '失败'), '字段错误')
  assert.equal(apiErrorMessage({ response: { data: { detail: { type: 'validation' } } } }, '失败'), '失败')
})

test('全链条归档面板只对全所范围的指定岗位和管理员开放', () => {
  assert.equal(canManageFullchainArchive('基础管控', 'user', [], ['police.dispatch.manage'], 'all', 'all'), true)
  assert.equal(canManageFullchainArchive('所队领导', 'user', [], ['police.dispatch.manage'], 'all', 'all'), true)
  assert.equal(canManageFullchainArchive('组员', 'super_admin', [], ['police.dispatch.manage'], 'all', 'all'), true)
  assert.equal(canManageFullchainArchive('组员', 'user', [], ['police.dispatch.manage'], 'all', 'all'), false)
  assert.equal(canManageFullchainArchive('基础管控', 'user', [], ['police.dispatch.manage'], 'own_department', 'all'), false)
})

test('归档候选使用后端分页、防竞态和跨页选择，历史展示安全逐条状态', () => {
  const panel = readFileSync(new URL('../src/components/FullchainArchivePanel.tsx', import.meta.url), 'utf8')
  const rawPanel = readFileSync(new URL('../src/components/FullchainPoliceRawPanel.tsx', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  const upload = readFileSync(new URL('../src/pages/DataUploadCenter.tsx', import.meta.url), 'utf8')
  const tasks = readFileSync(new URL('../src/pages/MobileTaskList.tsx', import.meta.url), 'utf8')

  assert.match(panel, /candidateRequestId/)
  assert.match(panel, /page_size: pageSize/)
  assert.match(panel, /preserveSelectedRowKeys: true/)
  assert.match(panel, /showTotal: value => `共 \$\{value\} 条`/)
  assert.match(panel, /item\.items\.filter\(detail => detail\.status !== 'success'\)/)
  assert.match(panel, /apiErrorMessage/)
  assert.match(rawPanel, /apiErrorMessage/)
  assert.match(client, /items: Array<\{/)
  assert.match(upload, /canUseFullchainArchive && <FullchainPoliceRawPanel enabled \/>/)
  assert.match(tasks, /canUseFullchainArchive && <FullchainArchivePanel \/>/)
})
