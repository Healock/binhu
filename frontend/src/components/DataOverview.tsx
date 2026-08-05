import type { CSSProperties, ReactNode } from 'react'
import { Skeleton, Statistic, Tooltip } from 'antd'
import { InfoCircleOutlined } from '@ant-design/icons'

export interface DataOverviewMetric {
  key: string
  title: string
  value: number | string
  suffix?: string
  precision?: number
  help?: string
  valueStyle?: CSSProperties
  onClick?: () => void
}

interface DataOverviewProps {
  rangeTitle?: string
  rangeValue?: ReactNode
  rangeDescription?: ReactNode
  metrics: DataOverviewMetric[]
  loading?: boolean
}

export default function DataOverview({
  rangeTitle,
  rangeValue,
  rangeDescription,
  metrics,
  loading = false,
}: DataOverviewProps) {
  const hasRange = Boolean(rangeTitle)
  const layoutClass = hasRange
    ? metrics.length <= 2
      ? 'grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4'
      : 'grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8'
    : 'grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6'

  return (
    <div className={layoutClass}>
      {hasRange && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 sm:col-span-2">
          <div className="text-xs text-slate-500">{rangeTitle}</div>
          {loading ? (
            <Skeleton className="mt-1.5" active paragraph={false} />
          ) : (
            <>
              <div className="mt-1.5 text-base font-semibold text-slate-900 sm:text-lg">
                {rangeValue}
              </div>
              {rangeDescription && (
                <div className="mt-1 text-xs text-slate-500">
                  {rangeDescription}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {metrics.map(metric => {
        const content = loading ? (
          <Skeleton active paragraph={false} />
        ) : (
          <Statistic
            title={(
              <span>
                {metric.title}
                {metric.help && (
                  <Tooltip title={metric.help}>
                    <InfoCircleOutlined className="ml-1 text-slate-400" />
                  </Tooltip>
                )}
              </span>
            )}
            value={metric.value}
            suffix={metric.suffix}
            precision={metric.precision}
            valueStyle={metric.valueStyle}
          />
        )
        return metric.onClick ? (
          <button
            key={metric.key}
            type="button"
            className="min-w-0 cursor-pointer rounded-lg border border-slate-200 bg-white p-3 text-left transition-colors hover:border-blue-300 hover:bg-blue-50/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
            onClick={metric.onClick}
            aria-label={`查看${metric.title}明细`}
          >
            {content}
          </button>
        ) : (
          <div
            key={metric.key}
            className="min-w-0 rounded-lg border border-slate-200 bg-white p-3"
          >
            {content}
          </div>
        )
      })}
    </div>
  )
}
