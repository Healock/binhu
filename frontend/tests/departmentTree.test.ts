import assert from 'node:assert/strict'
import test from 'node:test'
import {
  buildDepartmentPathMap,
  buildDepartmentCascader,
  departmentOptionLabel,
  selectableDepartments,
} from '../src/utils/departmentTree.ts'

const departments = [
  { id: 1, name: '社区警务队', type: 'unit', community_name: null, parent_id: null, area_id: null, is_active: true },
  { id: 2, name: '中片', type: 'unit', community_name: null, parent_id: 1, area_id: 10, is_active: true },
  { id: 3, name: '冬梅', type: 'community', community_name: '冬梅', parent_id: 2, area_id: null, is_active: true },
  { id: 4, name: '内勤', type: 'internal', community_name: null, parent_id: null, area_id: null, is_active: true },
] as const

test('department labels show the complete path without redundant type suffixes', () => {
  const paths = buildDepartmentPathMap(departments as never)
  assert.equal(paths.get(3), '社区警务队 / 中片 / 冬梅')
  assert.equal(departmentOptionLabel(departments[2] as never, paths), '社区警务队 / 中片 / 冬梅')
  assert.equal(departmentOptionLabel(departments[0] as never, paths), '社区警务队')
  assert.equal(departmentOptionLabel(departments[3] as never, paths), '内勤（历史部门）')
})

test('community cascader retains expandable ancestors but cannot select them', () => {
  const { options, paths } = buildDepartmentCascader(departments as never, [], true)
  assert.deepEqual(paths.get(3), [1, 2, 3])
  assert.equal(options.length, 1)
  assert.equal(options[0].selectable, false)
  assert.equal(options[0].disableCheckbox, true)
  const area = options[0].children![0]
  assert.equal(area.selectable, false)
  assert.equal(area.children![0].selectable, true)
  assert.equal(area.children![0].label, '冬梅')
})

test('unit cascader permits parent units and omits community targets', () => {
  const { options, paths } = buildDepartmentCascader(departments as never, [], false)
  assert.deepEqual([...paths.keys()].sort(), [1, 2])
  assert.equal(options[0].selectable, true)
  assert.equal(options[0].children![0].selectable, true)
  assert.equal(options[0].children![0].children, undefined)
})

test('inactive ancestors remain navigable and only selected historical targets survive', () => {
  const inactive = departments.map(item => ({ ...item, is_active: false }))
  const { options, paths } = buildDepartmentCascader(inactive as never, [3, 4], true)
  assert.deepEqual(paths.get(3), [1, 2, 3])
  assert.equal(options[0].disableCheckbox, true)
  assert.equal(options[1].label, '内勤（历史部门）（已停用）')
  assert.equal(options[1].selectable, true)
  assert.equal(buildDepartmentCascader(inactive as never, [], true).options.length, 0)
})

test('arbitrary depths, orphaned parents and cycles never lose a selectable department', () => {
  const deep = Array.from({ length: 12 }, (_, index) => ({
    ...departments[0], id: index + 10, name: `单位${index}`, parent_id: index ? index + 9 : null,
  }))
  const { paths } = buildDepartmentCascader(deep as never, [], false)
  assert.equal(paths.get(21)!.length, 12)
  const invalid = [
    { ...departments[0], id: 11, parent_id: 12 },
    { ...departments[0], id: 12, parent_id: 11 },
    { ...departments[0], id: 13, parent_id: 999 },
  ]
  const recovered = buildDepartmentCascader(invalid as never, [], false)
  assert.deepEqual(recovered.options.map(item => item.value), [11, 12, 13])
})

test('department choices follow the position scope and preserve selected legacy values', () => {
  const community = selectableDepartments(departments as never, [], true)
  assert.deepEqual(community.map(item => item.id), [3])
  const legacy = selectableDepartments(departments as never, [4], false)
  assert.deepEqual(legacy.map(item => item.id), [1, 2, 4])
})
