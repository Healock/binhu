import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { Alert, Button, Collapse, Empty, Input, Modal, Pagination, Radio, Select, Spin, Tag, message } from 'antd'
import { ArrowLeftOutlined, ArrowRightOutlined, CheckOutlined, HistoryOutlined, SearchOutlined } from '@ant-design/icons'
import { ListContent, ListToolbar, PageHeader } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import { useResponsiveLayout } from '../hooks/useResponsiveLayout'
import useDebouncedValue from '../hooks/useDebouncedValue'
import useSystemTime from '../hooks/useSystemTime'
import { confirmMobileTaskAddressMatch, getAddressAnnotationOptions, getMobileTaskDetail, listMobileTasks, markAddressManualUnmatched, resolveMobileTaskAddressConflict, getMobileTaskFilterOptions, type AddressAnnotationOptions, type MobileTaskDetailData, type MobileTaskItem } from '../api/client'
import { MOBILE_TASK_TYPES, canEditOnlineQuery } from '../utils/mobileTaskRouting'
import { ADDRESS_STATES, NO_MATCH_REASONS, PENDING_ADDRESS_STATES, readRecentAnnotations, saveRecentAnnotation, type AnnotationLocator } from '../utils/addressAnnotation'
import { setPendingNavigationChanges, setNavigationDecision } from '../utils/navigationGuard'

type Target = { parser_type: string; row_key: string }
type QueueFilter = { parser: string; community: string; state: string; keyword: string; page: number }
const stateOptions = [{ value: 'pending', label: '待处理' }, { value: 'suggested', label: '自动匹配' }, { value: 'confirmed', label: '已确认' }, { value: 'manual_unmatched', label: '无匹配小区' }, { value: 'all', label: '全部结果' }]
const sameTask = (a: Target | null, b: Target | null) => !!a && !!b && a.parser_type === b.parser_type && a.row_key === b.row_key
function safeError(error: any, fallback: string) { const detail = error?.response?.data?.detail; return typeof detail === 'string' ? detail : detail?.message || fallback }

export default function AddressConfirmation() {
  const navigate = useNavigate()
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const { user } = useAuth()
  const root = useRef<HTMLDivElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const layout = useResponsiveLayout(root)
  const formatDateTime = useSystemTime()
  const narrow = layout.width < 900
  const [queueMode, setQueueMode] = useState(false)
  const [filter, setFilter] = useState<QueueFilter>({ parser: params.get('parser_type') || MOBILE_TASK_TYPES[0], community: params.get('community') || '', state: params.get('state') || 'pending', keyword: '', page: 1 })
  const [communities, setCommunities] = useState<Array<{ value: string; label: string }>>([])
  const [rows, setRows] = useState<MobileTaskItem[]>([])
  const [total, setTotal] = useState(0)
  const [target, setTarget] = useState<Target | null>(() => params.get('row_key') ? { parser_type: params.get('parser_type') || MOBILE_TASK_TYPES[0], row_key: params.get('row_key')! } : null)
  const targetRef = useRef(target); targetRef.current = target
  const [detail, setDetail] = useState<MobileTaskDetailData | null>(null)
  const [options, setOptions] = useState<AddressAnnotationOptions | null>(null)
  const [query, setQuery] = useState('')
  const [optionPage, setOptionPage] = useState(1)
  const [selection, setSelection] = useState<number | undefined>()
  const [selectedName, setSelectedName] = useState('')
  const [resultType, setResultType] = useState<'community' | 'none'>('community')
  const [reason, setReason] = useState<string | undefined>()
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [optionsLoading, setOptionsLoading] = useState(false)
  const [error, setError] = useState('')
  const [versionConflict, setVersionConflict] = useState(false)
  const [queueError, setQueueError] = useState('')
  const [optionError, setOptionError] = useState('')
  const [recent, setRecent] = useState<AnnotationLocator[]>([])
  const [showRecent, setShowRecent] = useState(false)
  const [resume, setResume] = useState<Target | null>(null)
  const [completed, setCompleted] = useState(false)
  const [reload, setReload] = useState(0)
  const [optionReload, setOptionReload] = useState(0)
  const queueSequence = useRef(0), detailSequence = useRef(0), optionSequence = useRef(0)
  const keyword = useDebouncedValue(filter.keyword, 350), optionKeyword = useDebouncedValue(query, 250)
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty
  const canEditQuery = canEditOnlineQuery(user?.member?.position, user?.role, user?.permission_groups?.map(group => group.code), user?.permissions)
  const canManageLibrary = Boolean(user?.permissions?.includes('police.address.manage'))

  const allowLeave = useCallback(() => !savingRef.current && (!dirtyRef.current || window.confirm('小区选择尚未提交。确定放弃当前选择并离开吗？')), [])
  useEffect(() => {
    setPendingNavigationChanges(dirty || saving)
    const warn = (event: BeforeUnloadEvent) => { if (dirtyRef.current || savingRef.current) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener('beforeunload', warn)
    return () => { setPendingNavigationChanges(false); window.removeEventListener('beforeunload', warn) }
  }, [dirty, saving])
  useEffect(() => {
    setNavigationDecision(allowLeave)
    return () => setNavigationDecision(null)
  }, [allowLeave, location.key])
  useEffect(() => { setRecent(user?.id ? readRecentAnnotations(user.id) : []); setResume(null) }, [user?.id])

  const open = useCallback((next: Target, review = false) => {
    if (!allowLeave()) return
    setQueueMode(!review)
    if (review && targetRef.current && !sameTask(targetRef.current, next)) { const previousTarget = { ...targetRef.current }; setResume(previous => previous || previousTarget) }
    dirtyRef.current = false; setDirty(false); setSelection(undefined); setReason(undefined); setResultType('community')
    setQuery(''); setOptionPage(1); setSelectedName(''); setDetail(null); setOptions(null); setError(''); setVersionConflict(false); setOptionError(''); setCompleted(false)
    setTarget(next); setReload(value => value + 1)
    setParams(previous => { const updated = new URLSearchParams(previous); updated.set('parser_type', next.parser_type); updated.set('row_key', next.row_key); return updated }, { replace: true, state: location.state })
  }, [allowLeave, setParams, location.state])

  const fetchQueue = useCallback(async (pageOverride = filter.page) => {
    const sequence = ++queueSequence.current
    setLoading(true); setQueueError('')
    try {
      const response = await listMobileTasks({ parser_type: filter.parser, scope: 'community', status: 'all', communities: filter.community ? [filter.community] : [], match_status: filter.state === 'pending' ? PENDING_ADDRESS_STATES : filter.state === 'all' ? [] : [filter.state], keyword, sort: 'address_asc', page: pageOverride, page_size: 20 })
      if (sequence !== queueSequence.current) return null
      setRows(response.data); setTotal(response.total)
      return response
    } catch (failure) { if (sequence === queueSequence.current) setQueueError(safeError(failure, '任务队列加载失败，请重试')); return null }
    finally { if (sequence === queueSequence.current) setLoading(false) }
  }, [filter.parser, filter.community, filter.state, filter.page, keyword])
  useEffect(() => { void fetchQueue(); return () => { queueSequence.current++ } }, [fetchQueue])
  useEffect(() => {
    let active = true
    void getMobileTaskFilterOptions(filter.parser, 'community').then(value => { if (active) setCommunities(value.communities) }).catch(() => { if (active) setCommunities([]) })
    return () => { active = false }
  }, [filter.parser])

  useEffect(() => {
    const sequence = ++detailSequence.current
    optionSequence.current++
    if (!target) return
    setDetailLoading(true); setError(''); setOptions(null)
    void getMobileTaskDetail(target.parser_type, target.row_key).then(value => {
      if (sequence !== detailSequence.current) return
      setDetail(value)
      setSelection(value.address_match?.small_community_id || undefined)
      setSelectedName(value.address_match?.small_community_name || '')
      setResultType(value.address_match?.status === 'manual_unmatched' ? 'none' : 'community')
      setReason(undefined)
      setDirty(false)
      requestAnimationFrame(() => heading.current?.focus({ preventScroll: true }))
    }).catch(failure => { if (sequence === detailSequence.current) { setDetail(null); setError(safeError(failure, '任务不存在、已归档或不在当前账号权限范围内')) } })
      .finally(() => { if (sequence === detailSequence.current) setDetailLoading(false) })
    return () => { detailSequence.current++ }
  }, [target?.parser_type, target?.row_key, reload])

  useEffect(() => {
    if (!target || !detail) return
    const sequence = ++optionSequence.current
    setOptionsLoading(true); setOptionError('')
    void getAddressAnnotationOptions(target.parser_type, target.row_key, { keyword: optionKeyword, page: optionPage, page_size: 30, selected_id: detail.address_match?.small_community_id || undefined }).then(value => {
      if (sequence === optionSequence.current) { setOptions(value); if (!dirtyRef.current) setReason(value.manual_unmatched_reason || NO_MATCH_REASONS.find(item => item.label === detail.address_match?.reason)?.value) }
    }).catch(failure => { if (sequence === optionSequence.current) { setOptions(null); setOptionError(safeError(failure, '小区选项加载失败，请重试')) } })
      .finally(() => { if (sequence === optionSequence.current) setOptionsLoading(false) })
    return () => { optionSequence.current++ }
  }, [target?.parser_type, target?.row_key, detail, optionKeyword, optionPage, optionReload])

  const changeFilter = (patch: Partial<QueueFilter>) => {
    if (!allowLeave()) return
    dirtyRef.current = false; setDirty(false); setTarget(null); setDetail(null); setCompleted(false); setResume(null)
    const next = { ...filter, ...patch, page: patch.page || 1 }
    setFilter(next)
    setParams({ parser_type: next.parser, community: next.community, state: next.state }, { replace: true, state: location.state })
  }
  const leaveDetail = () => {
    if (!allowLeave()) return
    dirtyRef.current = false; setDirty(false); setTarget(null); setDetail(null); setResume(null)
    setParams(previous => { const next = new URLSearchParams(previous); next.delete('row_key'); return next }, { replace: true, state: location.state })
  }
  const returnToTasks = () => { if (allowLeave()) { dirtyRef.current = false; setDirty(false); setPendingNavigationChanges(false); if (location.state?.fromTask) navigate(-1); else navigate('/tasks') } }
  const match = detail?.address_match || detail?.task.address_match
  const label = ADDRESS_STATES[match?.status || 'unmatched'] || ADDRESS_STATES.unmatched
  const sourceProblem = Boolean(detail && (detail.task.conflict || detail.sources.length !== 1))
  const canConfirm = Boolean(options?.capabilities.confirm) && !sourceProblem
  const canMarkNone = Boolean(options?.capabilities.manual_unmatched) && !sourceProblem
  const candidates = (match?.candidates || []).map(item => ({ id: Number(item.entry_id), name: String(item.name || ''), community_name: String(item.community_name || '') })).filter(item => item.id > 0)
  const recommendations = candidates.filter(item => options?.community && item.community_name === options.community)
  const conflicts = candidates.filter(item => item.community_name && options?.community && item.community_name !== options.community)
  const updateDraftStatus = (type: 'community' | 'none', id = selection, code = reason) => {
    const initialType = match?.status === 'manual_unmatched' ? 'none' : 'community'
    const initialReason = options?.manual_unmatched_reason || NO_MATCH_REASONS.find(item => item.label === match?.reason)?.value
    const changed = type !== initialType || (type === 'community' ? id !== (match?.small_community_id || undefined) : code !== initialReason)
    dirtyRef.current = changed; setDirty(changed)
  }
  const selectCommunity = (id: number) => { updateDraftStatus('community', id); setSelection(id); setSelectedName(options?.items.find(item => item.id === id)?.name || recommendations.find(item => item.id === id)?.name || ''); setError('') }

  const submit = async (next: boolean, conflictId?: number) => {
    if (!detail || savingRef.current || versionConflict || !target) return
    const source = detail.sources.find(item => item.row_key === detail.task.row_key) || detail.sources[0]
    if (!source?.revision || !source.row_hash) { setError('任务缺少有效来源版本，请重新打开任务'); return }
    if (!conflictId && (resultType === 'community' ? !selection : !reason)) { setError(resultType === 'community' ? '请先选择一个小区' : '请选择无匹配小区的原因'); return }
    const savedTarget = { ...target }
    const oldIndex = rows.findIndex(item => sameTask(item, savedTarget))
    const following = oldIndex >= 0 ? rows[oldIndex + 1] : undefined
    savingRef.current = true; setSaving(true); setError('')
    let committed = false
    try {
      let resultStatus = resultType === 'none' ? 'manual_unmatched' : 'confirmed'
      if (conflictId) {
        await resolveMobileTaskAddressConflict(target.parser_type, target.row_key, source.id, conflictId, source.revision, source.row_hash)
        resultStatus = 'review'
      } else if (resultType === 'none') {
        await markAddressManualUnmatched(target.parser_type, target.row_key, { source_id: source.id, expected_revision: source.revision, expected_row_hash: source.row_hash, reason_code: reason! })
      } else await confirmMobileTaskAddressMatch(target.parser_type, target.row_key, source.id, selection!, source.revision, source.row_hash)
      committed = true
      setDirty(false); dirtyRef.current = false; setPendingNavigationChanges(false)
      if (user?.id) setRecent(saveRecentAnnotation(user.id, { ...savedTarget, result: resultStatus }))
      const refreshed = await fetchQueue()
      if (next && !conflictId) {
        // Prefer the next item from the original queue; an updated row may move to the first position.
        let nextTask: Target | undefined = following
        if (!nextTask && refreshed) {
          const movedOut = !refreshed.data.some(item => sameTask(item, savedTarget))
          nextTask = movedOut ? refreshed.data[oldIndex >= 0 ? oldIndex : 0] : undefined
          if (!nextTask && !movedOut && refreshed.total > filter.page * 20) {
            const pageData = await fetchQueue(filter.page + 1)
            if (pageData) { setFilter(value => ({ ...value, page: value.page + 1 })); nextTask = pageData.data[0] }
          }
        }
        savingRef.current = false
        if (nextTask) open(nextTask)
        else { setReload(value => value + 1); setCompleted(Boolean(refreshed)); if (!refreshed) setError('已保存，但队列刷新失败。请重试加载队列后继续。') }
      } else { setReload(value => value + 1); if (conflictId) message.success('任务社区已修正，请核对最新小区归属') }
    } catch (failure: any) {
      if (committed) setError('结果已保存，但后续加载失败。请查看最近处理，不要重复提交。')
      else if (failure?.response?.status === 409) {
        setVersionConflict(true)
        setError('任务已被更新。你的选择已保留；请读取最新版本，核对地址和社区后再提交。')
      } else setError(safeError(failure, '保存失败，选择已保留，请重试'))
    } finally { savingRef.current = false; setSaving(false) }
  }
  const refreshConflict = async () => {
    if (!target || savingRef.current) return
    const current = { ...target }, sequence = ++detailSequence.current
    setDetailLoading(true)
    try {
      const latest = await getMobileTaskDetail(current.parser_type, current.row_key)
      if (sequence !== detailSequence.current || !sameTask(targetRef.current, current)) return
      setDetail(latest); setVersionConflict(false)
      setError('已读取最新版本，请对照地址与社区重新核对保留的选择。确认无误后可再次提交。')
    } catch (failure) { if (sequence === detailSequence.current) setError(safeError(failure, '读取最新版本失败，选择已保留，请重试')) }
    finally { if (sequence === detailSequence.current) setDetailLoading(false) }
  }
  const resolveConflict = (id: number, community: string) => Modal.confirm({ title: '修正任务社区', content: `将任务社区从“${detail?.task.community || '未填写'}”改为“${community}”，然后重新匹配。原始地址保留，当前核查人不会自动重新分配。`, okText: '修正并重新匹配', cancelText: '取消', onOk: () => submit(false, id) })

  return <div ref={root} className={`app-page address-workbench ${narrow ? 'is-narrow' : ''} ${target ? 'has-selection' : ''}`}>
    <PageHeader title="确认地址" description="核对地址，选择小区。每次保存后都可以回看和重新标注。" />
    <div className="address-workbench-navigation">
      <Button aria-label="返回流口核查" icon={<ArrowLeftOutlined />} onClick={returnToTasks} disabled={saving}>返回流口核查</Button>
      <Button icon={<HistoryOutlined />} onClick={() => setShowRecent(value => !value)}>最近处理{recent.length ? `（${recent.length}）` : ''}</Button>
    </div>
    {showRecent && <section className="address-recent" aria-label="最近处理">
      <strong>最近处理 · 当前标签页</strong>
      <span className="address-muted">刷新后可通过“已确认”或“无匹配小区”筛选找回任务。</span>
      {!recent.length ? <span>暂未处理任务</span> : <div>{recent.map((item, index) => <Button key={`${item.parser_type}:${item.row_key}`} onClick={() => open(item, true)} disabled={saving}>{index === 0 ? '上一条' : `最近第 ${index + 1} 条`} · {item.parser_type} · {ADDRESS_STATES[item.result]?.text || '社区已修正'}</Button>)}</div>}
    </section>}
    <ListContent>
      <ListToolbar filters={<>
        <Select aria-label="业务类型" value={filter.parser} disabled={saving} onChange={parser => changeFilter({ parser, community: '' })} options={MOBILE_TASK_TYPES.map(value => ({ value, label: value }))} />
        <Select aria-label="社区" allowClear placeholder="全部授权社区" value={filter.community || undefined} disabled={saving} onChange={community => changeFilter({ community: community || '' })} options={communities} />
        <Select aria-label="处理状态" value={filter.state} disabled={saving} options={stateOptions} onChange={state => changeFilter({ state })} />
        <Input aria-label="搜索任务" allowClear prefix={<SearchOutlined />} placeholder="搜索姓名或地址" value={filter.keyword} disabled={saving} onChange={event => changeFilter({ keyword: event.target.value })} />
      </>} meta={<span>{loading ? '正在更新队列…' : `共 ${total} 条任务`}</span>} />
      {queueError && <Alert type="error" showIcon message={queueError} action={<Button onClick={() => void fetchQueue()}>重试加载</Button>} />}
      <div className="address-workbench-columns">
        <section className="address-queue" aria-label="地址任务队列">
          <div className="address-section-heading"><strong>任务队列</strong><span>选择一条开始核对</span></div>
          <Spin spinning={loading}><div className="address-queue-items">
            {!rows.length && !loading ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下没有任务" /> : rows.map(task => <button type="button" key={task.task_key} className={`address-queue-row ${sameTask(task, target) ? 'is-selected' : ''}`} aria-current={sameTask(task, target) ? 'true' : undefined} disabled={saving} onClick={() => open(task)}>
              <span className="address-queue-title"><strong>{task.summary.title || '未填写姓名'}</strong><Tag color={ADDRESS_STATES[task.address_match?.status || 'unmatched']?.color}>{ADDRESS_STATES[task.address_match?.status || 'unmatched']?.text || '待标注'}</Tag></span>
              <span className="address-queue-address">{task.summary.original_address || '未填写原始地址'}</span>
              <span className="address-muted">{task.community || '未填写社区'}{task.address_match?.small_community_name ? ` · ${task.address_match.small_community_name}` : ''}</span>
            </button>)}
          </div></Spin>
          {total > 20 && <Pagination simple current={filter.page} pageSize={20} total={total} disabled={saving} onChange={page => changeFilter({ page })} showSizeChanger={false} />}
        </section>
        <section className="address-review" aria-label="核对工作区">
          {recent[0] && <div className="address-saved" role="status"><CheckOutlined /><span>上一条已保存 · {ADDRESS_STATES[recent[0].result]?.text || '社区已修正'}</span><Button type="link" size="small" disabled={saving} onClick={() => open(recent[0], true)}>查看</Button></div>}
          {resume && !sameTask(resume, target) && <Button onClick={() => { if (allowLeave()) { const next = resume; dirtyRef.current = false; setResume(null); open(next) } }} disabled={saving}>返回刚才正在核对的任务</Button>}
          {narrow && target && <Button icon={<ArrowLeftOutlined />} onClick={leaveDetail} disabled={saving}>返回任务队列</Button>}
          {error && <Alert type="error" showIcon message={error} action={target && versionConflict && <Button disabled={saving || detailLoading} onClick={() => void refreshConflict()}>重新核对最新版本</Button>} />}
          {!target ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从左侧选择任务，开始核对小区归属" /> : detailLoading ? <div className="address-loading"><Spin aria-label="正在读取最新任务" /></div> : detail && <>
            <header className="address-review-heading"><div><span className="address-eyebrow">正在核对 · {detail.task.parser_type}</span><h2 ref={heading} tabIndex={-1} aria-live="polite">{detail.task.summary.title || '未填写姓名'}</h2></div><Tag color={label.color}>{label.text}</Tag></header>
            {!rows.some(item => sameTask(item, target)) && <p className="address-muted">此任务不在当前队列筛选中，正在按任务入口查看。</p>}
            <section className="address-facts" aria-label="核对地址">
              <div><span>原始地址</span><strong>{detail.task.summary.original_address || '未填写'}</strong></div>
              {(detail.sources[0]?.values?.['现住址']) && <div><span>现住址</span><strong>{detail.sources[0].values['现住址']}</strong></div>}
              <div><span>任务社区</span><strong>{detail.task.community || '未填写'}</strong></div>
              <div><span>当前小区</span><strong>{match?.small_community_name || (match?.status === 'manual_unmatched' ? '人工核对：无匹配小区' : '尚未确定')}</strong></div>
            </section>
            {optionError && <Alert type="error" message={optionError} action={<Button onClick={() => setOptionReload(value => value + 1)}>重新加载</Button>} />}
            <fieldset className="address-choice" disabled={saving || optionsLoading || !options}>
              <legend>选择核对结果</legend>
              <Radio.Group value={resultType} onChange={event => { setResultType(event.target.value); updateDraftStatus(event.target.value) }} options={[{ value: 'community', label: '归属具体小区', disabled: !canConfirm }, { value: 'none', label: '无匹配小区', disabled: !canMarkNone }]} />
              {resultType === 'community' ? <>
                {recommendations.length > 0 && <div className="address-recommendations"><span className="address-muted">推荐小区</span><div>{recommendations.map(item => <Button disabled={!canConfirm || saving} key={item.id} type={selection === item.id ? 'primary' : 'default'} onClick={() => selectCommunity(item.id)}>{item.name}</Button>)}</div></div>}
                {!recommendations.length && <p className="address-muted">暂无可靠推荐，可直接从本社区小区库选择。</p>}
                <label htmlFor="annotation-community-search">搜索{options?.community || '当前社区'}的小区</label>
                <Input id="annotation-community-search" prefix={<SearchOutlined />} allowClear value={query} disabled={!canConfirm || saving} placeholder="输入小区名称" onChange={event => { setQuery(event.target.value); setOptionPage(1) }} />
                <Spin spinning={optionsLoading}><Radio.Group className="address-community-options" value={selection} onChange={event => selectCommunity(event.target.value)} disabled={!canConfirm || saving}>
                  {options?.items.map(item => <Radio key={item.id} value={item.id}>{item.name}<span className="address-muted"> · {item.community_name}{options.items.filter(option => option.name === item.name).length > 1 ? ` · ${item.detail_address || '地址库编号 ' + item.id}` : ''}</span></Radio>)}
                </Radio.Group></Spin>
                {options && !options.items.length && <p>未找到符合条件的小区。可以调整搜索，或选择“无匹配小区”记录结论。</p>}
                {options && options.total > 30 && <Pagination simple pageSize={30} current={optionPage} total={options.total} disabled={saving} onChange={setOptionPage} showSizeChanger={false} />}
                {selection && <div className="address-selected">已选：{selectedName || '已选择小区，请清空搜索核对'}</div>}
              </> : <>
                <label htmlFor="annotation-reason">无匹配原因</label><Select id="annotation-reason" value={reason} disabled={!canMarkNone || saving} placeholder="选择最符合当前情况的原因" options={NO_MATCH_REASONS} onChange={value => { setReason(value); updateDraftStatus('none', selection, value) }} />
                <p className="address-muted">保留地址和任务社区，不创建“未知小区”。这条任务暂不能进行依赖小区归属的新分配；补齐信息后可以重新标注。</p>
              </>}
            </fieldset>
            {sourceProblem && <Alert type="warning" showIcon message="任务来源重复或异常，请联系基础管控或管理员核对本地来源记录。修复后重新打开本任务。" />}
            {options && !sourceProblem && !canConfirm && !canMarkNone && <Alert type="info" showIcon message="当前账号仅可查看，请联系组长或有权管理任务的上级岗位标注" />}
            {conflicts.length > 0 && <section className="address-conflicts"><strong>发现其他社区候选</strong><p>任务社区：{detail.task.community || '未填写'}。请核对归属后再修正社区。</p>{conflicts.map(item => <div key={item.id}><span>{item.name} · {item.community_name}</span>{options?.capabilities.resolve_conflict && !sourceProblem ? <Button size="small" disabled={saving || dirty} onClick={() => resolveConflict(item.id, item.community_name)}>修正到此社区并重新匹配</Button> : <span className="address-muted">联系基础管控或管理员处理</span>}</div>)}{dirty && <p>请先提交当前选择，或重新核对后再修正社区。</p>}</section>}
            <Collapse ghost items={[{ key: 'evidence', label: '查看匹配说明与处理记录', children: <div className="address-evidence"><p>{match?.reason || '尚无匹配说明'}</p>{options?.history?.map((item, index) => <p key={index}>{formatDateTime(item.created_at)} · {item.action || '记录无匹配小区'} {NO_MATCH_REASONS.find(reason => reason.value === item.reason_code)?.label || ''}</p>)}<div>{canEditQuery && <Button disabled={saving} onClick={() => { if (allowLeave()) navigate(`/query?type=${encodeURIComponent(detail.task.parser_type)}`) }}>在线数据查询</Button>}{canManageLibrary && <Button disabled={saving} onClick={() => { if (allowLeave()) navigate('/police-addresses') }}>维护小区地址库</Button>}</div></div> }]} />
            {completed && <Alert type="success" showIcon message={total === 0 ? '当前筛选已处理完，最后一条结果保留在此' : '已到当前队列末尾，最后一条结果保留在此'} action={<Button onClick={leaveDetail}>返回队列</Button>} />}
            <footer className="address-submit"><span aria-live="polite">{saving ? '正在保存，请稍候…' : dirty ? '选择尚未提交' : '核对后再提交'}</span><div>
              <Button disabled={saving || versionConflict || !(resultType === 'none' ? canMarkNone && reason : canConfirm && selection)} onClick={() => void submit(!queueMode)}>{!queueMode ? '确认并下一条' : '确认并停留'}</Button>
              <Button type="primary" aria-label={queueMode ? '确认并下一条' : '确认并停留'} icon={queueMode ? <ArrowRightOutlined /> : <CheckOutlined />} loading={saving} disabled={versionConflict || !(resultType === 'none' ? canMarkNone && reason : canConfirm && selection)} onClick={() => void submit(queueMode)}>{queueMode ? '确认并下一条' : '确认并停留'}</Button>
            </div></footer>
          </>}
        </section>
      </div>
    </ListContent>
  </div>
}
