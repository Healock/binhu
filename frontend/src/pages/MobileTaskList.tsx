import {
  ExclamationCircleOutlined,
  PhoneOutlined,
  RightOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { Alert, Button, Empty, Input, Segmented, Select, Skeleton, Tag, message } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  listMobileTasks,
  type MobileTaskItem,
  type MobileTaskReviewStage,
  type MobileTaskScope,
  type MobileTaskStatus,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { isFlowTaskAdmin, MOBILE_TASK_TYPES } from '../utils/mobileTaskRouting'
import {
  mobileTaskCanLaunchTelephone,
  mobileTaskPhoneOptions,
} from '../utils/mobileTasks'
import MobilePhonePicker from '../components/MobilePhonePicker'

const STATUS_OPTIONS = [
  { label: '待处理', value: 'pending' },
  { label: '需复核', value: 'review' },
  { label: '已完成', value: 'completed' },
  { label: '全部', value: 'all' },
]

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

export default function MobileTaskList() {
  const navigate = useNavigate()
  const { recordActivity, user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedType = searchParams.get('type') || MOBILE_TASK_TYPES[0]
  const parserType = MOBILE_TASK_TYPES.includes(requestedType as any)
    ? requestedType
    : MOBILE_TASK_TYPES[0]
  const requestedScope = searchParams.get('scope')
  const adminMode = isFlowTaskAdmin(
    user?.role,
    user?.permission_groups?.map(group => group.code),
  )
  const scope: MobileTaskScope = adminMode
    ? 'all'
    : requestedScope === 'community' ? 'community' : 'mine'
  const requestedStatus = searchParams.get('status')
  const requestedReviewStage = searchParams.get('review_stage')
  const [status, setStatus] = useState<MobileTaskStatus>(
    ['pending', 'review', 'completed', 'all'].includes(requestedStatus || '')
      ? requestedStatus as MobileTaskStatus
      : 'pending',
  )
  const [reviewStage, setReviewStage] = useState<MobileTaskReviewStage>(
    ['waiting_analysis', 'analyzed'].includes(requestedReviewStage || '')
      ? requestedReviewStage as MobileTaskReviewStage
      : 'all',
  )
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [rows, setRows] = useState<MobileTaskItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [sourceMessage, setSourceMessage] = useState('')

  const load = useCallback(async (targetPage = 1, append = false) => {
    append ? setLoadingMore(true) : setLoading(true)
    setError('')
    try {
      const result = await listMobileTasks({
        parser_type: parserType,
        scope,
        status,
        review_stage: reviewStage,
        keyword: keyword || undefined,
        page: targetPage,
      })
      setRows(current => append ? [...current, ...result.data] : result.data)
      setTotal(result.total)
      setPage(targetPage)
      setSourceMessage(result.message || '')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || reason?.message || '任务列表读取失败')
      if (!append) setRows([])
    } finally {
      setLoading(false)
      setLoadingMore(false)
    }
  }, [keyword, parserType, reviewStage, scope, status])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const next = new URLSearchParams()
    next.set('type', parserType)
    next.set('scope', scope)
    next.set('status', status)
    if (status === 'review' && reviewStage !== 'all') next.set('review_stage', reviewStage)
    setSearchParams(next, { replace: true })
  }, [parserType, reviewStage, scope, setSearchParams, status])

  const updateQuery = (type: string, nextScope: MobileTaskScope) => {
    const next = new URLSearchParams(searchParams)
    next.set('type', type)
    next.set('scope', nextScope)
    setSearchParams(next)
  }

  const dial = async (phone: string) => {
    await recordActivity().catch(() => {})
    const navigation = navigator as Navigator & { userAgentData?: { mobile?: boolean } }
    if (!mobileTaskCanLaunchTelephone(
      navigation.userAgent,
      navigation.userAgentData?.mobile,
      navigation.maxTouchPoints,
    )) {
      await navigator.clipboard.writeText(phone)
      message.info('当前设备没有拨号功能，已复制电话号码')
      return
    }
    window.location.href = `tel:${phone}`
  }

  return (
    <div className="mobile-task-page">
      <section className="app-card mobile-task-filter-card">
        <div className="flex items-center gap-3">
          <Select
            className="min-w-0 flex-1"
            size="large"
            value={parserType}
            onChange={value => updateQuery(value, scope)}
            options={MOBILE_TASK_TYPES.map(value => ({ value, label: value }))}
          />
          {adminMode ? (
            <Tag color="blue">全所</Tag>
          ) : (
            <Segmented
              className="mobile-task-scope-switch"
              value={scope}
              onChange={value => updateQuery(parserType, value as MobileTaskScope)}
              options={[{ label: '我的', value: 'mine' }, { label: '社区', value: 'community' }]}
            />
          )}
        </div>
        <div className="mobile-task-filter-card__row">
          <Segmented
            className="mobile-task-status-switch w-full"
            block
            value={status}
            onChange={value => setStatus(value as MobileTaskStatus)}
            options={STATUS_OPTIONS}
          />
        </div>
        {status === 'review' && (
          <div className="mobile-task-filter-card__row">
            <Segmented
              block
              className="w-full"
              value={reviewStage}
              onChange={value => setReviewStage(value as MobileTaskReviewStage)}
              options={[
                { label: '全部复核', value: 'all' },
                { label: '等待研判', value: 'waiting_analysis' },
                { label: '已研判', value: 'analyzed' },
              ]}
            />
          </div>
        )}
        <div className="mobile-task-filter-card__row flex gap-2">
          <Input
            allowClear
            value={keywordInput}
            prefix={<SearchOutlined />}
            placeholder="搜索姓名、电话或地址"
            onChange={event => setKeywordInput(event.target.value)}
            onPressEnter={() => setKeyword(keywordInput.trim())}
          />
          <Button type="primary" className="min-h-11" onClick={() => setKeyword(keywordInput.trim())}>查询</Button>
        </div>
      </section>

      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
      {sourceMessage && <Alert type="warning" showIcon message={sourceMessage} />}

      <div className="flex items-center justify-between px-1 text-sm text-[var(--app-text-secondary)]">
        <span>共 {total} 条</span>
        {keyword && <button type="button" className="text-[var(--app-primary)]" onClick={() => { setKeyword(''); setKeywordInput('') }}>清除搜索</button>}
      </div>

      {loading ? (
        <div className="mobile-task-list"><div className="app-card p-4"><Skeleton active paragraph={{ rows: 3 }} /></div><div className="app-card p-4"><Skeleton active paragraph={{ rows: 3 }} /></div></div>
      ) : rows.length === 0 ? (
        <div className="app-card py-8"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的任务" /></div>
      ) : (
        <div className="mobile-task-list">
          {rows.map(task => {
            const state = STATE_LABELS[task.state]
            const phoneOptions = mobileTaskPhoneOptions(task.summary.phone)
            const phoneDisplay = phoneOptions.length > 0
              ? phoneOptions.join('、')
              : task.summary.phone
            return (
              <article
                key={task.row_key}
                role="button"
                tabIndex={0}
                className="mobile-task-item-card"
                onClick={() => navigate(`/tasks/${encodeURIComponent(task.parser_type)}/${task.row_key}?scope=${scope}`)}
                onKeyDown={event => { if (event.key === 'Enter') navigate(`/tasks/${encodeURIComponent(task.parser_type)}/${task.row_key}?scope=${scope}`) }}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="truncate font-semibold text-[var(--app-text-strong)]">{task.summary.title}</h2>
                      <Tag color={state.color}>{state.text}</Tag>
                      {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
                      {task.review_stage === 'waiting_analysis' && <Tag color="volcano">等待研判</Tag>}
                      {task.review_stage === 'analyzed' && <Tag color="purple">已研判</Tag>}
                      {task.pending_sync && <Tag color="blue">待同步</Tag>}
                    </div>
                    <p className="mt-1 text-xs text-[var(--app-text-secondary)]">{task.community || '社区未填写'} · {task.inspector || '待分配'}</p>
                  </div>
                  <RightOutlined className="mt-1 shrink-0 text-[var(--app-text-muted)]" />
                </div>
                {(task.summary.identity_number || phoneDisplay || task.summary.source) && (
                  <dl className="mobile-task-item-card__details">
                    {task.summary.identity_number && (
                      <div className="mobile-task-item-card__detail-row">
                        <dt>身份证号</dt>
                        <dd>{task.summary.identity_number}</dd>
                      </div>
                    )}
                    {phoneDisplay && (
                      <div className="mobile-task-item-card__detail-row">
                        <dt>手机号</dt>
                        <dd>{phoneDisplay}</dd>
                      </div>
                    )}
                    {task.summary.source && (
                      <div className="mobile-task-item-card__detail-row">
                        <dt>来源</dt>
                        <dd>{task.summary.source}</dd>
                      </div>
                    )}
                  </dl>
                )}
                {task.summary.address && <p className="mobile-task-item-card__address line-clamp-2 text-sm text-[var(--app-text)]">{task.summary.address}</p>}
                {task.review_stage === 'analyzed' && task.summary.analysis && (
                  <div className="mobile-task-analysis">
                    <div className="mobile-task-analysis__label">研判结果</div>
                    <div className="mobile-task-analysis__value">{task.summary.analysis}</div>
                  </div>
                )}
                <div className="mobile-task-item-card__footer flex items-center justify-between gap-3 border-t border-[var(--app-border)]">
                  <div className="min-w-0 text-xs text-[var(--app-text-secondary)]">
                    {task.summary.date || (task.source_count > 1 ? `${task.source_count} 条腾讯来源` : '点击进入处理')}
                  </div>
                  <MobilePhonePicker
                    phones={phoneOptions}
                    mode="dial"
                    label={phoneOptions.length > 1 ? '选择拨打' : '拨打'}
                    className="mobile-phone-native-select--compact"
                    buttonProps={{
                      type: 'primary',
                      ghost: true,
                      className: 'min-h-11 shrink-0',
                      icon: <PhoneOutlined />,
                    }}
                    onSelect={phone => void dial(phone)}
                  />
                </div>
              </article>
            )
          })}
          {rows.length < total && (
            <Button block className="mobile-task-load-more min-h-11" loading={loadingMore} onClick={() => void load(page + 1, true)}>加载更多</Button>
          )}
        </div>
      )}
    </div>
  )
}
