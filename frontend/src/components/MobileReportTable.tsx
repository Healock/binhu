import { useEffect, useMemo } from 'react'
import { Empty, Input } from 'antd'
import type { TableColumnsType, TableProps } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import AppTable from './AppTable'

type ReportRow = Record<string, any>

interface Props {
  columns: string[]
  rows: ReportRow[]
  titleColumns: string[]
  resetKey: string
  fullColumns: TableColumnsType<ReportRow>
  fullSummary?: TableProps<ReportRow>['summary']
  rowKey: (row: ReportRow, index: number) => string | number
}

export default function MobileReportTable({
  columns,
  rows,
  titleColumns,
  resetKey,
  fullColumns,
  fullSummary,
  rowKey,
}: Props) {
  const [keyword, setKeyword] = useState('')

  useEffect(() => {
    setKeyword('')
  }, [resetKey])

  const filteredRows = useMemo(() => {
    const search = keyword.trim().toLocaleLowerCase('zh-CN')
    if (!search) return rows
    return rows.filter(row => titleColumns.some(column => (
      String(row[column] || '').toLocaleLowerCase('zh-CN').includes(search)
    )))
  }, [keyword, rows, titleColumns])

  const visibleColumns = columns.filter(column => column !== 'id')
  const fixedFullColumns = fullColumns.map((column, index) => ({
    ...column,
    fixed: index < titleColumns.length ? 'left' as const : column.fixed,
  }))

  return (
    <div className="mobile-report-table__layout">
      <section className="app-card app-card--padded space-y-3">
        <Input
          allowClear
          value={keyword}
          onChange={event => setKeyword(event.target.value)}
          prefix={<SearchOutlined className="text-slate-400" />}
          placeholder={`搜索${titleColumns.join('或')}`}
        />
        <div className="flex items-center justify-between gap-3 rounded-lg bg-blue-50 px-3 py-2.5">
          <div>
            <div className="text-xs text-blue-700">筛选后总计</div>
            <div className="mt-0.5 text-sm font-medium text-blue-950">
              {filteredRows.length} 条
            </div>
          </div>
        </div>
      </section>

      {filteredRows.length === 0 ? (
        <section className="app-card py-6">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的数据" />
        </section>
      ) : (
        <AppTable<ReportRow>
          key={`mobile-full-${resetKey}`}
          columns={fixedFullColumns}
          dataSource={filteredRows}
          rowKey={rowKey}
          reportGrid
          sticky
          summary={fullSummary}
          scroll={{ x: Math.max(visibleColumns.length * 112, 720) }}
        />
      )}
    </div>
  )
}
