import assert from 'node:assert/strict'
import test from 'node:test'
import {
  buildDepartmentPathMap,
  departmentOptionLabel,
  selectableDepartments,
} from '../src/utils/departmentTree.ts'

const departments = [
  { id: 1, name: '社区警务队', type: 'unit', community_name: null, parent_id: null, area_id: null, is_active: true },
  { id: 2, name: '中片', type: 'unit', community_name: null, parent_id: 1, area_id: 10, is_active: true },
  { id: 3, name: '冬梅', type: 'community', community_name: '冬梅', parent_id: 2, area_id: null, is_active: true },
  { id: 4, name: '内勤', type: 'internal', community_name: null, parent_id: null, area_id: null, is_active: true },
] as const

test('department labels include the complete tree path and level', () => {
  const paths = buildDepartmentPathMap(departments as never)
  assert.equal(paths.get(3), '社区警务队 / 中片 / 冬梅')
  assert.equal(departmentOptionLabel(departments[3] as never, paths), '内勤（历史部门）')
})

test('department choices follow the position scope and preserve selected legacy values', () => {
  const community = selectableDepartments(departments as never, [], true)
  assert.deepEqual(community.map(item => item.id), [3])
  const legacy = selectableDepartments(departments as never, [4], false)
  assert.deepEqual(legacy.map(item => item.id), [1, 2, 4])
})
