import { useMemo } from 'react'
import { Cascader } from 'antd'
import type { DepartmentOption } from '../api/client'
import { buildDepartmentCascader, type DepartmentCascaderOption } from '../utils/departmentTree'

export default function MemberDepartmentCascader({
  departments, value, onChange, communityPosition, multiple, disabled,
}: {
  departments: DepartmentOption[]
  value: number[]
  onChange: (ids: number[]) => void
  communityPosition: boolean
  multiple: boolean
  disabled?: boolean
}) {
  const { options, paths } = useMemo(
    () => buildDepartmentCascader(departments, value, communityPosition),
    [departments, value, communityPosition],
  )
  const selectedPaths = value.map(id => paths.get(id) || [id])
  const shared = {
    options,
    disabled,
    className: 'member-department-cascader',
    classNames: { popup: { root: 'member-department-cascader-popup' } },
    expandTrigger: 'click' as const,
    displayRender: (labels: string[]) => labels.join(' / '),
    showSearch: {
      filter: (input: string, path: DepartmentCascaderOption[]) =>
        Boolean(path[path.length - 1]?.selectable) && path.some(item => item.label.includes(input.trim())),
    },
    'aria-label': '所属部门',
  }
  return multiple ? (
    <Cascader<DepartmentCascaderOption>
      {...shared}
      multiple
      showCheckedStrategy={Cascader.SHOW_CHILD}
      value={selectedPaths}
      onChange={selected => onChange(selected.map(path => path[path.length - 1]))}
      maxTagCount="responsive"
      placeholder="逐级选择，可多选社区"
    />
  ) : (
    <Cascader<DepartmentCascaderOption>
      {...shared}
      value={selectedPaths[0] || []}
      changeOnSelect={!communityPosition}
      allowClear={!communityPosition}
      onChange={(selected, selectedOptions) => {
        if (!selected.length) onChange([])
        else if (selectedOptions[selectedOptions.length - 1]?.selectable) {
          onChange([selected[selected.length - 1]])
        }
      }}
      placeholder="逐级选择所属部门"
    />
  )
}
