import {
  Bar,
  BarChart,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

export interface MonoRoundedStackedBarDatum {
  label: string
  completed: number
  unable: number
  pending: number
  total: number
}

interface MonoRoundedStackedBarChartProps {
  data: MonoRoundedStackedBarDatum[]
  ariaLabel: string
}

interface TooltipItem {
  color?: string
  name?: string
  value?: number
}

function MonoStackedTooltip({
  active,
  label,
  payload,
}: {
  active?: boolean
  label?: string
  payload?: TooltipItem[]
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="mono-rounded-stacked-bar__tooltip">
      <strong>{label}</strong>
      {payload.map(item => (
        <span key={item.name}>
          <i style={{ background: item.color }} />
          {item.name}：{Number(item.value || 0)}
        </span>
      ))}
    </div>
  )
}

/**
 * Adapted from Amicro's MIT-licensed MonoRoundedStackedBarChart.
 * The platform variant is horizontal so all community names remain readable.
 */
export default function MonoRoundedStackedBarChart({
  data,
  ariaLabel,
}: MonoRoundedStackedBarChartProps) {
  const animate = typeof window === 'undefined'
    || !window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const rows = data.map(item => ({
    ...item,
    ratio: `${item.completed}/${item.total}`,
  }))
  const height = Math.max(320, rows.length * 32 + 30)
  // Keep a non-zero numeric domain even when a scoped community has no
  // completed or pending rows. Recharts otherwise collapses the horizontal
  // axis to 0 and renders labels without a visible chart area.
  const maxTotal = Math.max(...rows.map(item => Number(item.total) || 0), 1)

  return (
    <div className="mono-rounded-stacked-bar" aria-label={ariaLabel}>
      <div className="mono-rounded-stacked-bar__header">
        <div>
          <span className="mono-rounded-stacked-bar__eyebrow">社区完成情况</span>
          <strong>{rows.length} 个社区</strong>
        </div>
        <div className="mono-rounded-stacked-bar__legend" aria-label="图例">
          <span><i className="is-completed" />已完成</span>
          <span><i className="is-unable" />无法核实</span>
          <span><i className="is-pending" />其他待完成</span>
        </div>
      </div>

      <div className="mono-rounded-stacked-bar__stage" style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 8, right: 58, bottom: 8, left: 0 }}
            barCategoryGap={10}
          >
            <XAxis type="number" hide domain={[0, maxTotal]} />
            <YAxis
              type="category"
              dataKey="label"
              axisLine={false}
              tickLine={false}
              width={88}
              tick={{ fill: 'var(--app-text)', fontSize: 12, fontWeight: 600 }}
            />
            <Tooltip
              cursor={{ fill: 'var(--app-surface-muted)' }}
              content={<MonoStackedTooltip />}
            />
            <Bar
              dataKey="completed"
              name="已完成"
              stackId="community"
              fill="var(--mono-chart-completed)"
              radius={[999, 0, 0, 999]}
              isAnimationActive={animate}
              animationDuration={650}
            />
            <Bar
              dataKey="unable"
              name="无法核实"
              stackId="community"
              fill="var(--mono-chart-unable)"
              isAnimationActive={animate}
              animationDuration={650}
            />
            <Bar
              dataKey="pending"
              name="其他待完成"
              stackId="community"
              fill="var(--mono-chart-pending)"
              radius={[0, 999, 999, 0]}
              isAnimationActive={animate}
              animationDuration={650}
            >
              <LabelList
                dataKey="ratio"
                position="right"
                fill="var(--app-text-secondary)"
                fontSize={11}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <ul className="sr-only">
        {rows.map(item => (
          <li key={item.label}>
            {item.label}：已完成 {item.completed}，无法核实 {item.unable}，其他待完成 {item.pending}，共 {item.total}
          </li>
        ))}
      </ul>
    </div>
  )
}
