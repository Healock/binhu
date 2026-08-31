import { CheckSquareOutlined, CloseOutlined, TeamOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Modal, Progress, Select, Spin, Tabs, Tag, message } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  bulkAssignMobileTasks,
  cancelMobileTaskAssignments,
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

interface AssignmentOutcome {
  skipped: Array<{ row_key: string; reason: string }>
  failed: Array<{ row_key: string; reason: string }>
}

const MATCH_STATUS_LABELS: Record<string, { label: string; color: string }> = {
  confirmed: { label: '已人工确认', color: 'success' },
  suggested: { label: '自动匹配', color: 'processing' },
  ambiguous: { label: '多候选待确认', color: 'warning' },
  conflict: { label: '地址冲突', color: 'error' },
  invalid: { label: '无效地址', color: 'default' },
  unmatched: { label: '未关联小区', color: 'default' },
}

const isAssignableMatch = (status: string) => status === 'confirmed' || status === 'suggested'

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
  const [communities, setCommunities] = useState<Array<{ value: string; label: string; count: number; assigned_count?: number }>>([])
  const [inspectors, setInspectors] = useState<Record<string, string[]>>({})
  const [inspectorCounts, setInspectorCounts] = useState<Record<string, Record<string, number>>>({})
  const [availableTotal, setAvailableTotal] = useState(0)
  const [limited, setLimited] = useState(false)
  const [displayLimit, setDisplayLimit] = useState(2000)
  const [community, setCommunity] = useState('')
  const [smallCommunity, setSmallCommunity] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [assignOpen, setAssignOpen] = useState(false)
  const [inspector, setInspector] = useState<string>()
  const [progress, setProgress] = useState<AssignmentProgress | null>(null)
  const [outcome, setOutcome] = useState<AssignmentOutcome | null>(null)
  const dragRef = useRef<{ active: boolean; select: boolean }>({ active: false, select: true })
  const suppressClickRef = useRef(false)
  const loadRequestRef = useRef(0)

  const load = useCallback(async () => {
    const requestId = ++loadRequestRef.current
    setLoading(true)
    setError('')
    try {
      const result = await getMobileTaskAssignmentWorkbench(parserType)
      if (requestId !== loadRequestRef.current) return
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
      setSmallCommunity(current => (
        current && result.data.some(item => item.small_community === current)
          ? current
          : ''
      ))
      setSelected(current => {
        const valid = new Set(result.data.map(item => item.row_key))
        return new Set([...current].filter(key => valid.has(key)))
      })
    } catch (reason: any) {
      if (requestId !== loadRequestRef.current) return
      setError(reason?.response?.data?.detail || '未分配数据读取失败')
    } finally {
      if (requestId === loadRequestRef.current) setLoading(false)
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
    () => candidates.filter(item => (
      item.community === community
      && (!smallCommunity || (
        smallCommunity === '__unmatched__'
          ? !item.small_community
          : item.small_community === smallCommunity
      ))
    )),
    [candidates, community, smallCommunity],
  )
  const smallCommunityOptions = useMemo(() => {
    const counts = new Map<string, number>()
    candidates
      .filter(item => item.community === community)
      .forEach(item => {
        const key = item.small_community || '__unmatched__'
        counts.set(key, (counts.get(key) || 0) + 1)
      })
    return [...counts.entries()].map(([value, count]) => ({
      value,
      label: `${value === '__unmatched__' ? '未关联小区' : value}（${count}）`,
    }))
  }, [candidates, community])
  const inspectorOptions = inspectors[community] || []
  const assignableRemainingCount = useMemo(
    () => candidates.filter(item => (
      isAssignableMatch(item.match_status)
      && (inspectors[item.community] || []).length > 0
    )).length,
    [candidates, inspectors],
  )
  const selectedVisible = useMemo(
    () => visible.filter(item => selected.has(item.row_key)),
    [selected, visible],
  )
  const selectedInCommunity = useMemo(
    () => inspectorOptions.length > 0
      ? selectedVisible.filter(item => isAssignableMatch(item.match_status))
      : [],
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
    const skippedDetails: AssignmentOutcome['skipped'] = []
    const failedDetails: AssignmentOutcome['failed'] = []
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
        skippedDetails.push(...(result.details || []))
        failedDetails.push(...(result.failed_details || []))
        setOutcome({ skipped: [...skippedDetails], failed: [...failedDetails] })
        setCandidates(current => current.filter(item => !completedKeys.has(item.row_key)))
        setSelected(current => new Set([...current].filter(key => !completedKeys.has(key))))
        setProgress({ total: rowKeys.length, processed, updated, skipped, failed })
      }
      if (mode === 'balanced') message.success(`已平均分配 ${updated} 条剩余数据`)
      else message.success(`已分配 ${updated} 条数据给 ${assignedInspector}`)
      if (skipped || failed) message.warning(`另有 ${skipped + failed} 条数据已变化或写入失败，工作台已刷新`)
      setAssignOpen(false)
      setInspector(undefined)
      void load()
      void Promise.resolve(onChanged())
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '分配中断，已成功的数据不会重复分配')
      void load()
      void Promise.resolve(onChanged())
    } finally {
      setSaving(false)
    }
  }

  const runBalancedRemaining = async () => {
    const groups = communities
      .map(item => ({
        community: item.value,
        rowKeys: candidates
          .filter(candidate => (
            candidate.community === item.value
            && isAssignableMatch(candidate.match_status)
          ))
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
    const skippedDetails: AssignmentOutcome['skipped'] = []
    const failedDetails: AssignmentOutcome['failed'] = []
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
          skippedDetails.push(...(result.details || []))
          failedDetails.push(...(result.failed_details || []))
          setOutcome({ skipped: [...skippedDetails], failed: [...failedDetails] })
          setCandidates(current => current.filter(item => !completedKeys.has(item.row_key)))
          setSelected(current => new Set([...current].filter(key => !completedKeys.has(key))))
          setProgress({ total, processed, updated, skipped, failed })
        }
      }
      message.success(`已按社区平均分配 ${updated} 条剩余数据`)
      if (skipped || failed) message.warning(`另有 ${skipped + failed} 条数据已变化或写入失败，工作台已刷新`)
      void load()
      void Promise.resolve(onChanged())
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '平均分配中断，已成功的数据不会重复分配')
      void load()
      void Promise.resolve(onChanged())
    } finally {
      setSaving(false)
    }
  }

  const cancelAssigned = () => {
    if (!community) return
    const assignedCount = communities.find(item => item.value === community)?.assigned_count || 0
    if (!assignedCount) {
      message.info('当前社区没有可撤销的已分配数据')
      return
    }
    Modal.confirm({
      title: '撤销本社区已分配数据？',
      content: `将清除“${community}”中 ${assignedCount} 条未完成任务的核查人分配，核查结果、研判和历史记录不会删除。`,
      okText: '确认撤销',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setSaving(true)
        setError('')
        try {
          const result = await cancelMobileTaskAssignments(parserType, community)
          message.success(`已撤销 ${result.updated} 条分配`)
          if (result.skipped) message.warning(`另有 ${result.skipped} 条来源异常任务未处理`)
          void load()
          void Promise.resolve(onChanged())
        } catch (reason: any) {
          setError(reason?.response?.data?.detail || '撤销分配失败')
        } finally {
          setSaving(false)
        }
      },
    })
  }

  const close = () => {
    setAssignOpen(false)
    setInspector(undefined)
    if (!saving) {
      setSelected(new Set())
      setProgress(null)
      setOutcome(null)
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
      destroyOnClose={false}
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
                visible
                  .filter(item => isAssignableMatch(item.match_status))
                  .forEach(item => next.add(item.row_key))
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
            <Button
              danger
              disabled={!community || !(communities.find(item => item.value === community)?.assigned_count || 0) || saving}
              onClick={cancelAssigned}
            >
              撤销本社区已分配
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
              setSmallCommunity('')
              setSelected(new Set())
            }}
          />
        )}
        {community && (
          <div className="mobile-task-assignment-workbench__filters">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              value={smallCommunity || undefined}
              placeholder="筛选小区（含未关联）"
              options={smallCommunityOptions}
              onChange={value => {
                setSmallCommunity(value || '')
                setSelected(new Set())
              }}
            />
            <span>“自动匹配”和“已人工确认”的任务可直接分配。</span>
          </div>
        )}
        <div className="mobile-task-assignment-workbench__notice" aria-live="polite">
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
        </div>
        <div className="mobile-task-assignment-workbench__progress" aria-live="polite">
          {progress ? <>
            <Progress percent={Math.round(progress.processed / progress.total * 100)} status={saving ? 'active' : progress.failed ? 'exception' : 'normal'} />
            <span>已处理 {progress.processed}/{progress.total}，成功 {progress.updated}，跳过 {progress.skipped}，失败 {progress.failed}</span>
          </> : <span className="mobile-task-assignment-workbench__progress-idle">可拖动或点击选择待分配任务</span>}
        </div>
        {outcome && (outcome.skipped.length > 0 || outcome.failed.length > 0) && (
          <Alert
            type={outcome.failed.length ? 'error' : 'warning'}
            showIcon
            message="本次分配结果明细"
            description={(
              <div className="grid gap-1 text-sm">
                {outcome.failed.map(item => <div key={`failed-${item.row_key}`}>失败：{item.row_key} · {item.reason}</div>)}
                {outcome.skipped.map(item => <div key={`skipped-${item.row_key}`}>跳过：{item.row_key} · {item.reason}</div>)}
              </div>
            )}
          />
        )}

        <div className="mobile-task-assignment-workbench__scroll">
          <Spin spinning={loading && candidates.length === 0}>
            {visible.length ? (
              <div className="mobile-task-assignment-grid" onContextMenu={event => event.preventDefault()}>
              {visible.map(item => {
                const checked = selected.has(item.row_key)
                const canAssign = inspectorOptions.length > 0 && isAssignableMatch(item.match_status)
                const matchStatus = MATCH_STATUS_LABELS[item.match_status] || MATCH_STATUS_LABELS.unmatched
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
                      if (!canAssign) return
                      if (suppressClickRef.current) {
                        suppressClickRef.current = false
                        return
                      }
                      setSelectedState(item.row_key, !checked)
                    }}
                  >
                    <span className="mobile-task-assignment-item__source">
                      {mobileTaskSourceTags(item.source).map(value => <Tag key={value}>{value}</Tag>)}
                      <Tag color={matchStatus.color}>{matchStatus.label}</Tag>
                    </span>
                    <span>{item.small_community || '未关联小区'}</span>
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

        <footer className={`mobile-task-assignment-workbench__footer${selectedInCommunity.length ? '' : ' is-idle'}`}>
          {selectedInCommunity.length > 0 ? <>
            <span>已选择 {selectedInCommunity.length} 条 · {community}</span>
            <Button type="primary" size="large" onClick={() => setAssignOpen(true)}>分配核查人</Button>
          </> : <span>请选择可分配的任务</span>}
        </footer>
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
