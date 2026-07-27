import type { ReactNode } from 'react'
import { Empty, Table } from 'antd'
import type { TableProps } from 'antd'

type AppTableProps<T extends object> = TableProps<T> & {
  emptyText?: ReactNode
}

export default function AppTable<T extends object>({
  className = '',
  emptyText = '暂无数据',
  locale,
  pagination,
  scroll,
  size = 'small',
  ...props
}: AppTableProps<T>) {
  return (
    <div className="app-table-wrap app-table-wrap--antd">
      <Table<T>
        {...props}
        className={`app-data-table ${className}`.trim()}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={emptyText}
            />
          ),
          ...locale,
        }}
        pagination={pagination === undefined ? false : pagination}
        scroll={{ x: 'max-content', ...scroll }}
        showSorterTooltip={{ target: 'sorter-icon' }}
        size={size}
        sticky
      />
    </div>
  )
}
