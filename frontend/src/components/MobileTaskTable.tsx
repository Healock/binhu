import {
  CopyOutlined,
  EyeOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'
import { Button, Table, Tag, Tooltip, type TableColumnsType } from 'antd'
import type { Key } from 'react'
import type { MobileTaskItem } from '../api/client'
import {
  formatMobileTaskDeadline,
  mobileTaskPhoneOptions,
  mobileTaskSourceTags,
} from '../utils/mobileTasks'

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

interface MobileTaskTableProps {
  rows: MobileTaskItem[]
  total: number
  page: number
  loading: boolean
  selectionMode: boolean
  selectedRowKeys: Key[]
  canSelect: (task: MobileTaskItem) => boolean
  onSelect: (task: MobileTaskItem, selected: boolean) => void
  onOpen: (task: MobileTaskItem) => void
  onCopy: (value: string, label: '身份证号' | '手机号') => void
  onPageChange: (page: number) => void
}

function FilledField({ label, value }: { label: string; value: string }) {
  return (
    <div className="mobile-task-table-edit-field">
      <span>{label}</span>
      <strong title={value || '未填写'}>{value || '未填写'}</strong>
    </div>
  )
}

export default function MobileTaskTable({
  rows,
  total,
  page,
  loading,
  selectionMode,
  selectedRowKeys,
  canSelect,
  onSelect,
  onOpen,
  onCopy,
  onPageChange,
}: MobileTaskTableProps) {
  const columns: TableColumnsType<MobileTaskItem> = [
    {
      title: '截止日期',
      key: 'deadline',
      fixed: 'left',
      width: 100,
      render: (_, task) => formatMobileTaskDeadline(task.summary.deadline) || '-',
    },
    {
      title: '核查人',
      dataIndex: 'inspector',
      width: 105,
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">待分配</span>,
    },
    {
      title: '来源',
      key: 'source',
      width: 130,
      render: (_, task) => {
        const sources = mobileTaskSourceTags(task.summary.source)
        return sources.length
          ? <Tooltip title={sources.join('、')}><span className="block truncate">{sources.join('、')}</span></Tooltip>
          : <span className="text-[var(--app-text-muted)]">未填写</span>
      },
    },
    {
      title: '姓名',
      key: 'name',
      width: 110,
      render: (_, task) => (
        <button
          type="button"
          className="block max-w-full truncate text-left font-medium text-[var(--app-text-strong)] hover:text-[var(--app-primary)]"
          title={task.summary.title}
          onClick={() => onOpen(task)}
        >
          {task.summary.title || '未填写姓名'}
        </button>
      ),
    },
    {
      title: '身份证号码',
      key: 'identity_number',
      width: 190,
      render: (_, task) => task.summary.identity_number ? (
        <Button
          type="link"
          size="small"
          className="h-auto max-w-full p-0 text-xs"
          icon={<CopyOutlined />}
          onClick={() => onCopy(task.summary.identity_number, '身份证号')}
        >
          <span className="truncate">{task.summary.identity_number}</span>
        </Button>
      ) : <span className="text-[var(--app-text-muted)]">未填写</span>,
    },
    {
      title: '电话',
      key: 'phone',
      width: 150,
      render: (_, task) => {
        const phones = mobileTaskPhoneOptions(task.summary.phone)
        const phone = phones[0] || task.summary.phone
        if (!phone) return <span className="text-[var(--app-text-muted)]">未填写</span>
        return (
          <Button
            type="link"
            size="small"
            className="h-auto p-0"
            icon={<CopyOutlined />}
            onClick={() => onCopy(phone, '手机号')}
          >
            {phone}{phones.length > 1 ? ` +${phones.length - 1}` : ''}
          </Button>
        )
      },
    },
    {
      title: '地址',
      key: 'address',
      width: 250,
      ellipsis: true,
      render: (_, task) => {
        const address = task.summary.original_address || '未填写'
        return <Tooltip title={address}><span>{address}</span></Tooltip>
      },
    },
    {
      title: '登记情况',
      dataIndex: ['summary', 'registration_status'],
      width: 110,
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">未填写</span>,
    },
    {
      title: '状态',
      key: 'state',
      width: 190,
      render: (_, task) => {
        const state = STATE_LABELS[task.state]
        return (
          <div className="flex flex-wrap gap-1">
            <Tag color={state.color}>{state.text}</Tag>
            {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
            {task.review_stage === 'analyzed' && <Tag color="purple">已研判</Tag>}
            {task.photo_fetched && <Tag color="green">已调照片</Tag>}
            {(task.conflict || task.source_count > 1) && <Tag color="red">来源异常</Tag>}
            {task.pending_sync && <Tag color="blue">待同步</Tag>}
            {task.watch_marks?.map(mark => (
              <Tag key={`${task.row_key}-${mark.category_id}`} color={mark.color}>{mark.name}</Tag>
            ))}
          </div>
        )
      },
    },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 64,
      align: 'center',
      render: (_, task) => (
        <Tooltip title="查看任务">
          <Button
            type="text"
            icon={<EyeOutlined />}
            aria-label="查看任务"
            onClick={() => onOpen(task)}
          />
        </Tooltip>
      ),
    },
  ]

  return (
    <div className="app-card mobile-task-table overflow-hidden">
      <Table<MobileTaskItem>
        rowKey="row_key"
        size="middle"
        loading={loading}
        dataSource={rows}
        columns={columns}
        tableLayout="fixed"
        scroll={{ x: 1399 }}
        rowSelection={selectionMode ? {
          selectedRowKeys,
          hideSelectAll: true,
          columnWidth: 48,
          getCheckboxProps: task => ({ disabled: !canSelect(task) }),
          onSelect,
        } : undefined}
        expandable={{
          expandedRowKeys: rows.map(task => task.row_key),
          showExpandColumn: false,
          expandedRowRender: task => (
            <div
              className="mobile-task-table-edit-grid"
              role="button"
              tabIndex={0}
              onDoubleClick={() => onOpen(task)}
              onKeyDown={event => {
                if (event.key === 'Enter') onOpen(task)
              }}
            >
              <FilledField label="现住址" value={task.summary.current_address} />
              <FilledField label="核查结果" value={task.summary.result} />
              <FilledField label="研判" value={task.summary.analysis} />
              <FilledField label="二次反馈" value={task.summary.secondary_feedback} />
              <FilledField label="调取照片" value={task.photo_fetched ? '已调照片' : '未调照片'} />
            </div>
          ),
        }}
        pagination={{
          current: page,
          pageSize: 50,
          total,
          showSizeChanger: false,
          showTotal: count => `共 ${count} 条`,
          onChange: onPageChange,
        }}
        onRow={task => ({
          className: selectionMode && selectedRowKeys.includes(task.row_key)
            ? 'mobile-task-table-row-selected'
            : '',
          onDoubleClick: () => onOpen(task),
        })}
      />
    </div>
  )
}
