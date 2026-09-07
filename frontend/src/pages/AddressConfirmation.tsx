import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Alert, Button, Empty, Input, Modal, Pagination, Select, Spin, Tag, message } from 'antd'
import { CheckOutlined, SearchOutlined } from '@ant-design/icons'
import { ListContent, ListToolbar, PageHeader } from '../components/ui'
import useDebouncedValue from '../hooks/useDebouncedValue'
import { useAuth } from '../context/AuthContext'
import {
  confirmMobileTaskAddressMatch,
  getMobileTaskDetail,
  listMobileTasks,
  resolveMobileTaskAddressConflict,
  type MobileTaskDetailData,
  type MobileTaskItem,
} from '../api/client'
import { canEditOnlineQuery, MOBILE_TASK_TYPES } from '../utils/mobileTaskRouting'

const MATCH_STATUSES = ['ambiguous', 'conflict', 'unmatched', 'invalid']

function statusLabel(status: string) {
  return ({
    ambiguous: '多候选待确认',
    conflict: '地址冲突待处理',
    unmatched: '未匹配',
    invalid: '低信息地址',
  } as Record<string, string>)[status] || status
}

/** 独立地址标注工作台；流口任务页面只展示匹配结果，不在任务卡片内确认。 */
export default function AddressConfirmation() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const canEditQuery = canEditOnlineQuery(
    user?.member?.position,
    user?.role,
    user?.permission_groups?.map(group => group.code),
    user?.permissions,
  )
  const canManageAddressLibrary = Boolean(user?.permissions?.includes('police.address.manage'))
  const [parserType, setParserType] = useState<string>(MOBILE_TASK_TYPES[0])
  const [status, setStatus] = useState<string[]>(MATCH_STATUSES)
  const [keyword, setKeyword] = useState('')
  const [rows, setRows] = useState<MobileTaskItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [detail, setDetail] = useState<MobileTaskDetailData | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const debouncedKeyword = useDebouncedValue(keyword, 350)
  const requestSequenceRef = useRef(0)
  const detailRequestSequenceRef = useRef(0)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    const requestSequence = ++requestSequenceRef.current
    try {
      const result = await listMobileTasks({
        parser_type: parserType,
        scope: 'community',
        status: 'all',
        match_status: status,
        keyword: debouncedKeyword,
        sort: 'updated_desc',
        page,
        page_size: 20,
      })
      if (requestSequence !== requestSequenceRef.current) return
      setRows(result.data)
      setTotal(result.total)
    } catch (reason: any) {
      if (requestSequence !== requestSequenceRef.current) return
      setRows([])
      setTotal(0)
      setError(reason?.response?.data?.detail || '地址标注任务加载失败')
    } finally {
      setLoading(false)
    }
  }, [debouncedKeyword, page, parserType, status])

  useEffect(() => { void load() }, [load])

  const openDetail = async (task: MobileTaskItem) => {
    const requestSequence = ++detailRequestSequenceRef.current
    setDetail(null)
    setDetailOpen(true)
    setDetailLoading(true)
    try {
      const result = await getMobileTaskDetail(task.parser_type, task.row_key)
      if (requestSequence === detailRequestSequenceRef.current) setDetail(result)
    } catch (reason: any) {
      if (requestSequence === detailRequestSequenceRef.current) {
        message.error(reason?.response?.data?.detail || '任务详情加载失败')
      }
    } finally {
      if (requestSequence === detailRequestSequenceRef.current) setDetailLoading(false)
    }
  }

  const closeDetail = () => {
    detailRequestSequenceRef.current += 1
    setDetailOpen(false)
    setDetail(null)
    setDetailLoading(false)
  }

  const candidates = useMemo(() => {
    const values = detail?.address_match?.candidates || detail?.task.address_match?.candidates || []
    return values.map(item => ({
      id: Number(item.entry_id || 0),
      name: String(item.name || '未命名小区'),
      community: String(item.community_name || ''),
      score: Number(item.score || 0),
    })).filter(item => item.id > 0)
  }, [detail])

  const confirm = async (entryId: number) => {
    if (!detail) return
    const source = detail.sources.find(item => item.row_key === detail.task.row_key) || detail.sources[0]
    if (!source?.revision || !source.row_hash) {
      message.error('当前任务缺少版本信息，请刷新后重试')
      return
    }
    setConfirming(true)
    try {
      if (detail.address_match?.status === 'conflict') {
        await resolveMobileTaskAddressConflict(
          detail.task.parser_type,
          detail.task.row_key,
          source.id,
          entryId,
          source.revision,
          source.row_hash,
        )
      } else {
        await confirmMobileTaskAddressMatch(
          detail.task.parser_type,
          detail.task.row_key,
          source.id,
          entryId,
          source.revision,
          source.row_hash,
        )
      }
      message.success('小区归属已确认')
      closeDetail()
      await load()
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '确认失败，请刷新后重试')
    } finally {
      setConfirming(false)
    }
  }

  return (
    <div className="app-page address-confirmation-page grid gap-3 md:gap-4">
      <PageHeader
        title="确认地址"
        description="集中处理待人工标注的小区归属；自动匹配结果无需人工确认。"
      />
      <ListToolbar filters={(
        <>
          <Select
            value={parserType}
            onChange={value => { setParserType(value); setPage(1) }}
            options={MOBILE_TASK_TYPES.map(value => ({ value, label: value }))}
            className="min-w-48"
          />
          <Select
            mode="multiple"
            value={status}
            onChange={value => { setStatus(value); setPage(1) }}
            options={MATCH_STATUSES.map(value => ({ value, label: statusLabel(value) }))}
            className="min-w-56"
            placeholder="匹配状态"
          />
          <Input
            allowClear
            prefix={<SearchOutlined />}
            value={keyword}
            onChange={event => { setKeyword(event.target.value); setPage(1) }}
            placeholder="搜索姓名、地址或任务"
            className="min-w-64 max-w-md"
          />
        </>
      )} />
      {error && <Alert type="error" showIcon message={error} />}
      <ListContent inset>
        <Spin spinning={loading}>
          <div className="address-confirmation-list grid gap-3 md:gap-4">
          {rows.length === 0 ? <Empty description="没有待人工标注任务" /> : rows.map(task => {
            const match = task.address_match
            return (
              <div key={task.task_key} className="address-confirmation-row flex flex-wrap items-center justify-between gap-3 border-b border-[var(--app-border)] pb-3 last:border-0">
                <div className="grid min-w-0 gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <strong>{task.summary.title || '未填写姓名'}</strong>
                    <Tag>{statusLabel(match?.status || 'unmatched')}</Tag>
                  </div>
                  <span className="text-sm text-[var(--app-text-secondary)]">{task.summary.original_address || '未填写地址'}</span>
                  <span className="text-xs text-[var(--app-text-muted)]">任务社区：{task.community || '未填写'}</span>
                </div>
                <Button icon={<CheckOutlined />} onClick={() => void openDetail(task)}>核对候选</Button>
              </div>
            )
          })}
          </div>
        </Spin>
        {total > 20 && <Pagination current={page} pageSize={20} total={total} onChange={setPage} showSizeChanger={false} />}
      </ListContent>
      <Modal
        open={detailOpen}
        title="核对小区归属"
        onCancel={closeDetail}
        footer={null}
        width={640}
      >
        {detailLoading ? <Spin /> : detail && (
          <div className="address-confirmation-detail grid gap-3 md:gap-4">
            <div className="grid gap-1 text-sm">
              <strong>{detail.task.summary.title || '未填写姓名'}</strong>
              <span>原始地址：{detail.task.summary.original_address || '未填写'}</span>
              <span>任务社区：{detail.task.community || '未填写'}</span>
              <span className="text-xs text-[var(--app-text-secondary)]">确认前会检查任务是否已更新；如有变化，请重新打开核对后再确认。</span>
            </div>
            {candidates.length === 0 ? (
              <div className="grid gap-3">
                <Empty description="没有可安全确认的候选小区" />
                <Alert
                  type="warning"
                  showIcon
                  message="需要先补充可执行的处理条件"
                  description={canEditQuery || canManageAddressLibrary
                    ? '可以在在线数据查询修改当前地址，或维护小区地址库后重新匹配。'
                    : '当前账号没有修改入口，请联系基础管控或管理员处理地址和小区地址库。'}
                  action={(
                    <div className="flex flex-wrap gap-2">
                      {canEditQuery && (
                        <Button size="small" onClick={() => navigate(`/query?type=${encodeURIComponent(detail.task.parser_type)}`)}>在线数据查询修改地址</Button>
                      )}
                      {canManageAddressLibrary && (
                        <Button size="small" onClick={() => navigate('/police-addresses')}>维护小区地址库</Button>
                      )}
                    </div>
                  )}
                />
              </div>
            ) : (
              <div className="grid gap-3">
                {detail.address_match?.status === 'conflict' && (
                  <Alert
                    type="error"
                    showIcon
                    message="任务社区与候选小区所属社区冲突"
                    description={detail.address_match.reason || '请核对候选社区后再修正任务社区。'}
                  />
                )}
                {candidates.map(candidate => (
                  <div key={candidate.id} className="address-confirmation-candidate flex flex-wrap items-center justify-between gap-3 rounded border border-[var(--app-border)] p-3">
                    <div className="grid min-w-0 gap-1">
                      <strong>{candidate.name}</strong>
                      <span className="text-sm text-[var(--app-text-secondary)]">候选所属社区：{candidate.community || '未标注社区'} · 匹配度 {Math.round(candidate.score * 100)} 分</span>
                    </div>
                    <Button type="primary" loading={confirming} onClick={() => void confirm(candidate.id)}>
                      {detail.address_match?.status === 'conflict' ? '按候选社区修正' : '确认归属'}
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  )
}
