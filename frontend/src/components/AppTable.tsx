import type { ReactNode } from 'react'
import { Descriptions, Empty, Table } from 'antd'
import type { TableProps } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useResponsiveLayout } from '../hooks/useResponsiveLayout'
import { getHiddenResponsiveColumns, getResponsiveColumns, renderResponsiveColumnValue, type ResponsiveColumn } from './responsiveTable'

type AppTableProps<T extends object> = TableProps<T> & {
  emptyText?: ReactNode
  reportGrid?: boolean
  responsive?: boolean
  responsiveDetails?: boolean
  fitHeight?: boolean
}

export default function AppTable<T extends object>({
  className = '',
  emptyText = '暂无数据',
  locale,
  pagination,
  reportGrid = false,
  scroll,
  size = 'small',
  sticky = false,
  tableLayout,
  responsive = true,
  responsiveDetails = false,
  fitHeight = false,
  ...props
}: AppTableProps<T>) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [availableHeight, setAvailableHeight] = useState<number>()
  const layout = useResponsiveLayout(containerRef)
  const sourceColumns = useMemo(() => (props.columns || []) as ColumnsType<T>, [props.columns])
  const columns = useMemo(
    () => responsive ? getResponsiveColumns(sourceColumns, layout.mode) : sourceColumns,
    [layout.mode, responsive, sourceColumns],
  )
  const hiddenColumns = useMemo(
    () => responsive ? getHiddenResponsiveColumns(sourceColumns, layout.mode) : [],
    [layout.mode, responsive, sourceColumns],
  )
  const expandable = responsiveDetails && hiddenColumns.length
    ? {
        ...props.expandable,
        expandedRowRender: props.expandable?.expandedRowRender || ((record: T) => (
          <Descriptions
            size="small"
            column={1}
            items={hiddenColumns.map((column, index) => ({
              key: String(column.key ?? (column as { dataIndex?: unknown }).dataIndex ?? index),
              label: typeof column.title === 'string' ? column.title : `字段 ${index + 1}`,
              children: renderResponsiveColumnValue(column as ResponsiveColumn<T>, record),
            }))}
          />
        )),
      }
    : props.expandable
  useEffect(() => {
    if (!fitHeight) return
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const top = containerRef.current?.getBoundingClientRect().top ?? 0
        const paginationReserve = pagination === false || pagination === undefined ? 24 : 82
        const next = Math.max(220, Math.round(window.innerHeight - top - paginationReserve))
        setAvailableHeight(previous => previous === next ? previous : next)
      })
    }
    update()
    window.addEventListener('resize', update)
    const observer = containerRef.current && typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(update)
      : undefined
    if (observer && containerRef.current) observer.observe(containerRef.current)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', update)
      observer?.disconnect()
    }
  }, [fitHeight, layout.height, pagination])
  const tableScroll = {
    x: 'max-content' as const,
    ...(fitHeight && availableHeight ? { y: availableHeight } : {}),
    ...scroll,
  }
  return (
    <div ref={containerRef} className={`app-table-wrap app-table-wrap--antd app-table-wrap--${layout.mode} ${sticky ? 'app-table-wrap--sticky' : ''}`.trim()}>
      <Table<T>
        {...props}
        columns={columns}
        expandable={expandable}
        className={`app-data-table ${
          reportGrid ? 'app-data-table--report-grid' : ''
        } ${className}`.trim()}
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
        scroll={tableScroll}
        showSorterTooltip={{ target: 'sorter-icon' }}
        size={size}
        sticky={sticky}
        tableLayout={tableLayout ?? (reportGrid ? 'fixed' : undefined)}
      />
    </div>
  )
}
