import { CheckSquareOutlined, CloseOutlined, TeamOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Modal, Progress, Select, Spin, Tabs, Tag, message } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  bulkAssignMobileTasks,
  getMobileTaskAssignmentWorkbench,
  MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE,
  type MobileTaskAssignmentCandidate,
} from '../api/client'
import { mobileTaskSourceTags } from '../utils/mobileTasks'

interface AssignmentProgress {
  total: number
  processed: number
  updated: number
  skipped: number
  failed: number
}

export default function MobileTaskAssignmentWorkbench({
  open,
  parserType,
  onClose,
  onChanged,
}: {
  open: boolean
  parserType: string
  onClose: () => void
  onChanged: () => Promise<void> | void
}) {
  const [candidates, setCandidates] = useState<MobileTaskAssignmentCandidate[]>([])
  const [communities, setCommunities] = useState<Array<{ value: string; label: string; count: number }>>([])
  const [inspectors, setInspectors] = useState<Record<string, string[]>>({})
  const [inspectorCounts, setInspectorCounts] = useState<Record<string, Record<string, number>>>({})
  const [availableTotal, setAvailableTotal] = useState(0)
  const [limited, setLimited] = useState(false)
  const [displayLimit, setDisplayLimit] = useState(2000)
  const [community, setCommunity] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [assignOpen, setAssignOpen] = useState(false)
  const [inspector, setInspector] = useState<string>()
  const [progress, setProgress] = useState<AssignmentProgress | null>(null)
  const dragRef = useRef<{ active: boolean; select: boolean }>({ active: false, select: true })
  const suppressClickRef = useRef(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await getMobileTaskAssignmentWorkbench(parserType)
      setCandidates(result.data)
      setCommunities(result.communities)
      setInspectors(result.inspectors_by_community)
      setInspectorCounts(result.inspector_counts_by_community || {})
      setAvailableTotal(result.total || result.data.length)
      setLimited(Boolean(result.limited))
      setDisplayLimit(result.limit || 2000)
      setCommunity(current => (
        current && result.communities.some(item => item.value === current)
          ? current
          : result.communities[0]?.value || ''
      ))
      setSelected(current => {
        const valid = new Set(result.data.map(item => item.row_key))
        return new Set([...current].filter(key => valid.has(key)))
      })
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '未分配数据读取失败')
    } finally {
      setLoading(false)
    }
  }, [parserType])

  useEffect(() => {
    if (open) void load()
  }, [load, open])

  useEffect(() => {
    const finishDrag = () => { dragRef.current.active = false }
    window.addEventListener('pointerup', finishDrag)
    window.addEventListener('pointercancel', finishDrag)
    return () => {
      window.removeEventListener('pointerup', finishDrag)
      window.removeEventListener('pointercancel', finishDrag)
    }
  }, [])

  const visible = useMemo(
    () => candidates.filter(item => item.community === community),
    [candidates, community],
  )
  const inspectorOptions = inspectors[community] || []
  const assignableRemainingCount = useMemo(
    () => candidates.filter(item => (inspectors[item.community] || []).length > 0).length,
    [candidates, inspectors],
  )
  const selectedVisible = useMemo(
    () => visible.filter(item => selected.has(item.row_key)),
    [selected, visible],
  )
  const selectedInCommunity = useMemo(
    () => inspectorOptions.length > 0 ? selectedVisible : [],
    [inspectorOptions.length, selectedVisible],
  )

  const setSelectedState = (rowKey: string, checked: boolean) => {
    setSelected(current => {
      const next = new Set(current)
      if (checked) next.add(rowKey)
      else next.delete(rowKey)
      return next
    })
  }

  const runAssignment = async (
    rowKeys: string[],
    mode: 'single' | 'balanced',
    assignedInspector?: string,
  ) => {
    if (!rowKeys.length) return
    setSaving(true)
    setProgress({ total: rowKeys.length, processed: 0, updated: 0, skipped: 0, failed: 0 })
    let processed = 0
    let updated = 0
    let skipped = 0
    let failed = 0
    const completedKeys = new Set<string>()
    try {
      for (let offset = 0; offset < rowKeys.length; offset += MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE) {
        const chunk = rowKeys.slice(offset, offset + MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE)
        const result = await bulkAssignMobileTasks(parserType, {
          row_keys: chunk,
          inspector: mode === 'single' ? assignedInspector : undefined,
          mode,
          balanced_offset: mode === 'balanced' ? offset : undefined,
          balanced_total: mode === 'balanced' ? rowKeys.length : undefined,
        })
        const unresolved = new Set([
          ...result.details.map(item => item.row_key),
          ...result.failed_details.map(item => item.row_key),
        ])
        chunk.filter(key => !unresolved.has(key)).forEach(key => completedKeys.add(key))
        processed += chunk.length
        updated += result.updated
        skipped += result.skipped
        failed += result.failed
        setCandidates(current => current.filter(item => !completedKeys.has(item.row_key)))
        setSelected(current => new Set([...current].filter(key => !completedKeys.has(key))))
        setProgress({ total: rowKeys.length, processed, updated, skipped, failed })
      }
      if (mode === 'balanced') message.success(`已平均分配 ${updated} 条剩余数据`)
      else message.success(`已分配 ${updated} 条数据给 ${assignedInspector}`)
      if (skipped || failed) message.warning(`另有 ${skipped + failed} 条数据已变化或写入失败，工作台已刷新`)
      setAssignOpen(false)
      setInspector(undefined)
      await load()
      await onChanged()
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '分配中断，已成功的数据不会重复分配')
      await load()
      await onChanged()
    } finally {
      setSaving(false)
    }
  }

  const runBalancedRemaining = async () => {
    const groups = communities
      .map(item => ({
        community: item.value,
        rowKeys: candidates
          .filter(candidate => candidate.community === item.value)
          .map(candidate => candidate.row_key),
      }))
      .filter(group => group.rowKeys.length && (inspectors[group.community] || []).length)
    const total = groups.reduce((sum, group) => sum + group.rowKeys.length, 0)
    if (!total) return
    setSaving(true)
    setError('')
    setProgress({ total, processed: 0, updated: 0, skipped: 0, failed: 0 })
    let processed = 0
    let updated = 0
    let skipped = 0
    let failed = 0
    try {
      for (const group of groups) {
        for (let offset = 0; offset < group.rowKeys.length; offset += MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE) {
          const chunk = group.rowKeys.slice(offset, offset + MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE)
          const result = await bulkAssignMobileTasks(parserType, {
            row_keys: chunk,
            mode: 'balanced',
            balanced_offset: offset,
            balanced_total: group.rowKeys.length,
          })
          const unresolved = new Set([
            ...result.details.map(item => item.row_key),
            ...result.failed_details.map(item => item.row_key),
          ])
          const completedKeys = new Set(chunk.filter(key => !unresolved.has(key)))
          processed += chunk.length
          updated += result.updated
          skipped += result.skipped
          failed += result.failed
          setCandidates(current => current.filter(item => !completedKeys.has(item.row_key)))
          setSelected(current => new Set([...current].filter(key => !completedKeys.has(key))))
          setProgress({ total, processed, updated, skipped, failed })
        }
      }
      message.success(`已按社区平均分配 ${updated} 条剩余数据`)
      if (skipped || failed) message.warning(`另有 ${skipped + failed} 条数据已变化或写入失败，工作台已刷新`)
      await load()
      await onChanged()
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '平均分配中断，已成功的数据不会重复分配')
      await load()
      await onChanged()
    } finally {
      setSaving(false)
    }
  }

  const close = () => {
    setAssignOpen(false)
    setInspector(undefined)
    if (!saving) {
      setSelected(new Set())
      setProgress(null)
    }
    onClose()
  }

  return (
    <Modal
      open={open}
      title="分配数据"
      width="100vw"
      footer={null}
      closable
      closeIcon={<CloseOutlined />}
      onCancel={close}
      destroyOnClose
      className="mobile-task-assignment-modal"
      style={{ top: 0, maxWidth: '100vw', paddingBottom: 0 }}
      styles={{
        content: { height: '100vh', borderRadius: 0 },
        body: { height: 'calc(100vh - 55px)', padding: 0, overflow: 'hidden' },
      }}
    >
      <div className="mobile-task-assignment-workbench">
        <header className="mobile-task-assignment-workbench__toolbar">
          <div>
            <strong>未分配核查人的数据</strong>
            <span>按地址排序，只展示来源和地址</span>
          </div>
          <div className="mobile-task-assignment-workbench__actions">
            <Button icon={<CloseOutlined />} disabled={saving} onClick={close}>
              退出分配
            </Button>
            <Button
              icon={<CheckSquareOutlined />}
              disabled={!visible.length || !inspectorOptions.length}
              onClick={() => setSelected(current => {
                const next = new Set(current)
                visible.forEach(item => next.add(item.row_key))
                return next
              })}
            >
              全选
            </Button>
            <Button disabled={!selectedVisible.length} onClick={() => setSelected(new Set())}>清空选择</Button>
            <Button
              icon={<TeamOutlined />}
              disabled={!assignableRemainingCount}
              onClick={() => void runBalancedRemaining()}
            >
              平均分配剩余数据
            </Button>
          </div>
        </header>

        {communities.length > 1 && (
          <Tabs
            activeKey={community}
            items={communities.map(item => ({
              key: item.value,
              label: <span>{item.label}<Tag className="ml-1">{item.count}</Tag></span>,
            }))}
            onChange={value => {
              setCommunity(value)
              setSelected(new Set())
            }}
          />
        )}
        {community && !inspectorOptions.length && visible.length > 0 && <Alert type="warning" showIcon message="该社区当前没有可用的在岗核查人，数据暂时不能分配" />}
        {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>刷新</Button>} />}
        {limited && !error && (
          <Alert
            type="warning"
            showIcon
            message={`当前有 ${availableTotal} 条未分配数据，暂显示前 ${displayLimit} 条。分配完成后请刷新继续处理。`}
            action={<Button size="small" onClick={() => void load()}>刷新下一批</Button>}
          />
        )}
        {progress && (
          <div className="mobile-task-assignment-workbench__progress">
            <Progress percent={Math.round(progress.processed / progress.total * 100)} status={saving ? 'active' : progress.failed ? 'exception' : 'normal'} />
            <span>已处理 {progress.processed}/{progress.total}，成功 {progress.updated}，跳过 {progress.skipped}，失败 {progress.failed}</span>
          </div>
        )}

        <div className="mobile-task-assignment-workbench__scroll">
          <Spin spinning={loading || saving}>
            {visible.length ? (
              <div className="mobile-task-assignment-grid" onContextMenu={event => event.preventDefault()}>
              {visible.map(item => {
                const checked = selected.has(item.row_key)
                const canAssign = inspectorOptions.length > 0
                return (
                  <button
                    type="button"
                    key={item.row_key}
                    className={`mobile-task-assignment-item${checked ? ' is-selected' : ''}${canAssign ? '' : ' is-disabled'}`}
                    aria-pressed={checked}
                    aria-disabled={!canAssign}
                    onPointerDown={event => {
                      if (event.button !== 0 || !canAssign) return
                      if (event.pointerType === 'touch') {
                        suppressClickRef.current = false
                        return
                      }
                      event.preventDefault()
                      suppressClickRef.current = true
                      dragRef.current = { active: true, select: !checked }
                      if (canAssign) setSelectedState(item.row_key, !checked)
                    }}
                    onPointerEnter={() => {
                      if (dragRef.current.active) setSelectedState(item.row_key, dragRef.current.select)
                    }}
                    onClick={event => {
                      event.preventDefault()
                      if (suppressClickRef.current) {
                        suppressClickRef.current = false
                        return
                      }
                      setSelectedState(item.row_key, !checked)
                    }}
                  >
                    <span className="mobile-task-assignment-item__source">
                      {mobileTaskSourceTags(item.source).map(value => <Tag key={value}>{value}</Tag>)}
                    </span>
                    <strong>{item.address}</strong>
                  </button>
                )
              })}
              </div>
            ) : (
              <Empty description={community ? `${community}已没有未分配数据` : '当前没有未分配数据'} />
            )}
          </Spin>
        </div>

        {selectedInCommunity.length > 0 && (
          <footer className="mobile-task-assignment-workbench__footer">
            <span>已选择 {selectedInCommunity.length} 条 · {community}</span>
            <Button type="primary" size="large" onClick={() => setAssignOpen(true)}>分配核查人</Button>
          </footer>
        )}
      </div>

      <Modal
        open={assignOpen}
        title="选择核查人"
        okText="确认分配"
        cancelText="取消"
        confirmLoading={saving}
        okButtonProps={{ disabled: !inspector }}
        onOk={() => void runAssignment(selectedInCommunity.map(item => item.row_key), 'single', inspector)}
        onCancel={() => !saving && setAssignOpen(false)}
      >
        <Select
          className="w-full"
          size="large"
          showSearch
          optionFilterProp="label"
          placeholder="请选择本社区在岗核查人"
          value={inspector}
          options={inspectorOptions.map(value => ({
            value,
            label: `${value} · 已分配 ${inspectorCounts[community]?.[value] || 0} 条`,
          }))}
          onChange={setInspector}
        />
      </Modal>
    </Modal>
  )
}
