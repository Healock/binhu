import assert from 'node:assert/strict'
import test from 'node:test'
import { getUserDisplayName } from '../src/types/index.ts'

test('姓名、关联人员和用户名按优先级显示', () => {
  assert.equal(getUserDisplayName({
    username: 'login-name',
    display_name: '平台姓名',
    member: { id: 1, name: '人员姓名', position: '组员' },
  }), '平台姓名')
  assert.equal(getUserDisplayName({
    username: 'login-name',
    display_name: '',
    member: { id: 1, name: '人员姓名', position: '组员' },
  }), '人员姓名')
  assert.equal(getUserDisplayName({
    username: 'login-name',
    display_name: '',
    member: null,
  }), 'login-name')
})
