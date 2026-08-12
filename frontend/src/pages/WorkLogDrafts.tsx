import {
  Button,
  Card,
  DatePicker,
  Empty,
  Input,
  Modal,
  Pagination,
  Skeleton,
  Space,
  Tag,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  FileAddOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import dayjs from 'dayjs'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  deleteWorkLogDraft,
  listWorkLogDrafts,
} from '../api/client'
import AppTable from '../components/AppTable'
import { PageHeader, Panel } from '../components/ui'
import type { WorkLogDraftSummary } from '../types'
import useSystemTime from '../hooks/useSystemTime'

const PAGE_SIZE = 20

interface DraftFilters {
  startDate: string
  endDate: string
  keyword: string
}

const EMPTY_FILTERS: DraftFilters = {
  startDate: '',
  endDate: '',
  keyword: '',
}

function errorMessage(error: unknown, fallback: string) {
  if (axios.isAxiosError(error)) {
    return error.response?.data?.detail || fallback
  }
  return error instanceof Error ? error.message : fallback
}

export default function WorkLogDrafts() {
  const navigate = useNavigate()
  const formatTime = useSystemTime()
  const [modal, contextHolder] = Modal.useModal()
  const [formFilters, setFormFilters] = useState<DraftFilters>(EMPTY_FILTERS)
  const [queryFilters, setQueryFilters] = useState<DraftFilters>(EMPTY_FILTERS)
  const [drafts, setDrafts] = useState<WorkLogDraftSummary[]>([])
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  const loadDrafts = useCallback(async () => {
    setLoading(true)
    try {
      const result = await listWorkLogDrafts({
        page,
        page_size: PAGE_SIZE,
        start_date: queryFilters.startDate || undefined,
        end_date: queryFilters.endDate || undefined,
        keyword: queryFilters.keyword.trim() || undefined,
      })
      setDrafts(result.data)
      setTotal(result.total)
    } catch (error) {
      message.error(errorMessage(error, '读取工作日志草稿失败'))
    } finally {
      setLoading(false)
    }
  }, [page, queryFilters])

  useEffect(() => {
    void loadDrafts()
  }, [loadDrafts])

  const openDraft = (draft: WorkLogDraftSummary) => {
    navigate(`/work-log?date=${draft.business_date}`)
  }

  const confirmDelete = (draft: WorkLogDraftSummary) => {
    modal.confirm({
      title: `删除 ${draft.business_date} 的日报草稿？`,
      content: (
        <div className="space-y-2 text-sm">
          <p>删除后无法恢复，同一天可以重新创建一份日报草稿。</p>
          <p className="text-slate-500">
            只会删除这份草稿，不会影响在线数据、走访数据或已经生成的统计报表。
          </p>
        </div>
      ),
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setDeletingId(draft.id)
        try {
          await deleteWorkLogDraft(draft.id)
          message.success('草稿已删除')
          if (drafts.length === 1 && page > 1) {
            setPage(current => current - 1)
          } else {
            await loadDrafts()
          }
        } catch (error) {
          message.error(errorMessage(error, '删除草稿失败'))
          throw error
        } finally {
          setDeletingId(null)
        }
      },
    })
  }

  const columns = useMemo<TableColumnsType<WorkLogDraftSummary>>(() => [
    {
      title: '业务日期',
      dataIndex: 'business_date',
      width: 130,
      render: value => <span className="font-medium">{value}</span>,
    },
    {
      title: '类型',
      dataIndex: 'report_type',
      width: 90,
      render: () => <Tag>日报</Tag>,
    },
    {
      title: '创建人',
      dataIndex: ['creator', 'username'],
      width: 130,
    },
    {
      title: '当前编辑人',
      dataIndex: ['owner', 'username'],
      width: 130,
    },
    {
      title: '最后更新',
      dataIndex: 'updated_at',
      width: 190,
      render: formatTime,
    },
    {
      title: '最近导出',
      dataIndex: 'last_export_at',
      width: 190,
      render: formatTime,
    },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 190,
      render: (_, draft) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openDraft(draft)}
          >
            打开
          </Button>
          <Button
            danger
            type="link"
            size="small"
            icon={<DeleteOutlined />}
            loading={deletingId === draft.id}
            onClick={() => confirmDelete(draft)}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ], [deletingId, drafts.length, loadDrafts, page])

  const search = () => {
    setPage(1)
    setQueryFilters({
      ...formFilters,
      keyword: formFilters.keyword.trim(),
    })
  }

  const reset = () => {
    setFormFilters(EMPTY_FILTERS)
    setPage(1)
    setQueryFilters(EMPTY_FILTERS)
  }

  return (
    <div className="app-page min-w-0">
      {contextHolder}
      <PageHeader
        title="工作日志草稿"
        description="集中查看、打开和删除已经创建的日报草稿"
        actions={(
          <Button
            type="primary"
            icon={<FileAddOutlined />}
            onClick={() => navigate('/work-log')}
          >
            新建或填写日报
          </Button>
        )}
      />

      <Panel
        title="全部草稿"
        description="可以按业务日期、创建人或当前编辑人查找"
      >
        <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-center">
          <div className="flex min-w-0 items-center gap-2 md:hidden">
            <input
              type="date"
              aria-label="开始日期"
              value={formFilters.startDate}
              onChange={event => {
                const startDate = event.target.value
                setFormFilters(current => ({
                  ...current,
                  startDate,
                  endDate: current.endDate && current.endDate < startDate
                    ? startDate
                    : current.endDate,
                }))
              }}
              className="min-h-11 min-w-0 flex-1 rounded-md border border-slate-300 bg-transparent px-3 text-sm dark:border-slate-600"
            />
            <span className="text-xs text-slate-400">至</span>
            <input
              type="date"
              aria-label="结束日期"
              value={formFilters.endDate}
              onChange={event => {
                const endDate = event.target.value
                setFormFilters(current => ({
                  ...current,
                  startDate: current.startDate && current.startDate > endDate
                    ? endDate
                    : current.startDate,
                  endDate,
                }))
              }}
              className="min-h-11 min-w-0 flex-1 rounded-md border border-slate-300 bg-transparent px-3 text-sm dark:border-slate-600"
            />
          </div>
          <DatePicker.RangePicker
            className="hidden w-[300px] md:flex"
            allowClear
            value={
              formFilters.startDate && formFilters.endDate
                ? [dayjs(formFilters.startDate), dayjs(formFilters.endDate)]
                : null
            }
            onChange={(_, dateStrings) => {
              setFormFilters(current => ({
                ...current,
                startDate: dateStrings[0],
                endDate: dateStrings[1],
              }))
            }}
          />
          <Input
            allowClear
            className="w-full md:w-64"
            placeholder="创建人或当前编辑人"
            value={formFilters.keyword}
            onChange={event => setFormFilters(current => ({
              ...current,
              keyword: event.target.value,
            }))}
            onPressEnter={search}
          />
          <Space>
            <Button type="primary" icon={<SearchOutlined />} onClick={search}>
              查询
            </Button>
            <Button onClick={reset}>重置</Button>
          </Space>
        </div>

        <div className="hidden md:block">
          <AppTable
            columns={columns}
            dataSource={drafts}
            emptyText="没有符合条件的草稿"
            loading={loading}
            pagination={{
              current: page,
              pageSize: PAGE_SIZE,
              total,
              hideOnSinglePage: true,
              showSizeChanger: false,
              showTotal: count => `共 ${count} 份`,
              onChange: setPage,
            }}
            rowKey="id"
            scroll={{ x: 1050 }}
          />
        </div>

        <div className="md:hidden">
          {loading ? (
            <Card size="small">
              <Skeleton active paragraph={{ rows: 5 }} />
            </Card>
          ) : drafts.length === 0 ? (
            <Card size="small">
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="没有符合条件的草稿"
              />
            </Card>
          ) : (
            <div className="space-y-3">
              {drafts.map(draft => (
                <Card key={draft.id} size="small">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-base font-semibold">
                          {draft.business_date}
                        </span>
                        <Tag>日报</Tag>
                      </div>
                      <div className="mt-2 space-y-1 text-sm text-slate-500">
                        <div>创建人：{draft.creator.display_name || draft.creator.username}</div>
                        <div>当前编辑人：{draft.owner.display_name || draft.owner.username}</div>
                        <div>最后更新：{formatTime(draft.updated_at)}</div>
                        <div>最近导出：{formatTime(draft.last_export_at)}</div>
                      </div>
                    </div>
                  </div>
                  <div className="mt-3 flex gap-2 border-t border-slate-100 pt-3 dark:border-slate-700">
                    <Button
                      className="flex-1"
                      icon={<EditOutlined />}
                      onClick={() => openDraft(draft)}
                    >
                      打开
                    </Button>
                    <Button
                      danger
                      className="flex-1"
                      icon={<DeleteOutlined />}
                      loading={deletingId === draft.id}
                      onClick={() => confirmDelete(draft)}
                    >
                      删除
                    </Button>
                  </div>
                </Card>
              ))}
            </div>
          )}

          {!loading && total > PAGE_SIZE && (
            <div className="mt-4 flex justify-center">
              <Pagination
                simple
                current={page}
                pageSize={PAGE_SIZE}
                total={total}
                showSizeChanger={false}
                onChange={setPage}
              />
            </div>
          )}
        </div>
      </Panel>
    </div>
  )
}
