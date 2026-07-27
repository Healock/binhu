import type { TableColumnsType } from 'antd'
import AppTable from './AppTable'
import type { StatsItem } from '../types'

function fmtRate(v: number): string {
  return (v * 100).toFixed(1) + '%'
}

function rateColor(v: number): string {
  if (v >= 0.8) return 'text-green-600 font-medium'
  if (v >= 0.5) return 'text-yellow-600 font-medium'
  return 'text-red-600 font-medium'
}

interface Props {
  data: StatsItem[]
  loading: boolean
  page: number
  total: number
  pageSize: number
  onPageChange: (page: number) => void
}

export default function PivotTable({ data, loading, page, total, pageSize, onPageChange }: Props) {
  const columns: TableColumnsType<StatsItem> = [
    { title: '核查人', dataIndex: '核查人', key: '核查人', width: 120, sorter: (left, right) => left.核查人.localeCompare(right.核查人, 'zh-CN') },
    { title: '下发日期', dataIndex: '下发日期', key: '下发日期', width: 120, sorter: (left, right) => left.下发日期.localeCompare(right.下发日期) },
    { title: '数据总数', dataIndex: '数据总数', key: '数据总数', width: 110, sorter: (left, right) => left.数据总数 - right.数据总数 },
    { title: '已核查', dataIndex: '已核查', key: '已核查', width: 100, sorter: (left, right) => left.已核查 - right.已核查, render: value => <span className="text-green-600">{value}</span> },
    { title: '未核查', dataIndex: '未核查', key: '未核查', width: 100, sorter: (left, right) => left.未核查 - right.未核查, render: value => <span className="text-red-500">{value}</span> },
    { title: '核查完成率', dataIndex: '核查完成率', key: '核查完成率', width: 130, sorter: (left, right) => left.核查完成率 - right.核查完成率, render: value => <span className={rateColor(value)}>{fmtRate(value)}</span> },
    { title: '无法核实', dataIndex: '无法核实', key: '无法核实', width: 110, sorter: (left, right) => left.无法核实 - right.无法核实 },
    { title: '移交', dataIndex: '移交', key: '移交', width: 90, sorter: (left, right) => left.移交 - right.移交 },
    { title: '已登记', dataIndex: '已登记', key: '已登记', width: 100, sorter: (left, right) => left.已登记 - right.已登记 },
    { title: '通勤', dataIndex: '通勤', key: '通勤', width: 90, sorter: (left, right) => left.通勤 - right.通勤 },
    { title: '离苏', dataIndex: '离苏', key: '离苏', width: 90, sorter: (left, right) => left.离苏 - right.离苏 },
    { title: '无法见底数', dataIndex: '无法见底数', key: '无法见底数', width: 120, sorter: (left, right) => left.无法见底数 - right.无法见底数, render: value => <span className="font-medium text-orange-600">{value}</span> },
    { title: '核查见底率', dataIndex: '核查见底率', key: '核查见底率', width: 130, sorter: (left, right) => left.核查见底率 - right.核查见底率, render: value => <span className={rateColor(value)}>{fmtRate(value)}</span> },
    { title: '更新时间', dataIndex: 'computed_at', key: 'computed_at', width: 180, sorter: (left, right) => (left.computed_at || '').localeCompare(right.computed_at || ''), render: value => <span className="text-xs text-slate-400">{value || '-'}</span> },
  ]

  return (
    <AppTable<StatsItem>
      columns={columns}
      dataSource={data}
      emptyText="暂无数据，请先同步数据"
      loading={loading}
      onChange={pagination => {
        if (pagination.current && pagination.current !== page) onPageChange(pagination.current)
      }}
      pagination={{
        current: page,
        pageSize,
        total,
        hideOnSinglePage: true,
        showSizeChanger: false,
        showTotal: count => `共 ${count} 条记录`,
      }}
      rowKey={row => `${row.核查人}-${row.下发日期}-${row.computed_at || ''}`}
      scroll={{ x: 1590 }}
    />
  )
}
