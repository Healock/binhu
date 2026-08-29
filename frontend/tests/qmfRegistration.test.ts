import assert from 'node:assert/strict'
import test from 'node:test'

import {
  normalizeQmfCommunityCodeInput,
} from '../src/utils/qmfRegistration.ts'

test('全民防社区代码保留十位大写字母和数字', () => {
  assert.equal(normalizeQmfCommunityCodeInput(' 320584037c '), '320584037C')
  assert.equal(normalizeQmfCommunityCodeInput('320584037-'), '320584037')
  assert.equal(normalizeQmfCommunityCodeInput('320584037C99'), '320584037C')
})
