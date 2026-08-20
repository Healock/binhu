import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  Label,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

export interface MonoSparklinePoint {
  label: string
  value: number
}
export interface MonoTrendSeries {
  key: string
  label: string
  color: string
}

export interface MonoTrendPoint {
  label: string
  [key: string]: string | number
}

export interface MonoStatusDatum {
  label: string
  value: number
  color: string
}

interface MonoChartTooltipProps {
  active?: boolean
  label?: string
  payload?: Array<{ name?: string; value?: number; color?: string }>
}

function MonoChartTooltip({ active, label, payload }: MonoChartTooltipProps) {
  if (!active || !payload?.length) return null
  return (
    <div className="mono-business-chart__tooltip">
      {label && <strong>{label}</strong>}
      {payload.map((item, index) => (
        <span key={`${item.name || 'value'}-${index}`}>
          <i style={{ background: item.color || 'var(--app-primary)' }} />
          {item.name || '数值'}：{Number(item.value || 0).toLocaleString()}
        </span>
      ))}
    </div>
  )
}

export function MonoKpiSparkline({
  data,
  color = 'var(--mono-chart-primary)',
  label = '近 7 日趋势',
}: {
  data: MonoSparklinePoint[]
  color?: string
  label?: string
}) {
  return (
    <div className="mono-business-chart mono-business-chart--sparkline" aria-label={label}>
      <div className="mono-business-chart__caption">{label}</div>
      <div className="mono-business-chart__sparkline-stage">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 6, right: 4, bottom: 0, left: 4 }}>
            <defs>
              <linearGradient id="mono-sparkline-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.34} />
                <stop offset="100%" stopColor={color} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <Tooltip content={<MonoChartTooltip />} cursor={false} />
            <Area
              type="monotone"
              dataKey="value"
              name="工作量"
              stroke={color}
              strokeWidth={2.5}
              fill="url(#mono-sparkline-fill)"
              dot={{ r: 2.5, fill: color, strokeWidth: 0 }}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div className="mono-business-chart__axis-labels">
        <span>{data[0]?.label || ''}</span>
        <span>{data[data.length - 1]?.label || ''}</span>
      </div>
    </div>
  )
}

export function MonoBulletChart({
  actual,
  target,
  label,
  unit = '条',
  color = 'var(--mono-chart-primary)',
}: {
  actual: number
  target: number
  label: string
  unit?: string
  color?: string
}) {
  const safeTarget = Math.max(Number(target) || 0, 1)
  const safeActual = Math.max(Number(actual) || 0, 0)
  const percent = Math.min(100, Math.round((safeActual / safeTarget) * 100))
  return (
    <div className="mono-business-chart mono-business-chart--bullet" aria-label={`${label} ${safeActual}/${target}${unit}`}>
      <div className="mono-business-chart__header">
        <span>{label}</span>
        <strong>{safeActual.toLocaleString()} / {Number(target || 0).toLocaleString()} {unit}</strong>
      </div>
      <div className="mono-bullet__track">
        <span className="mono-bullet__fill" style={{ width: `${percent}%`, background: color }} />
        <span className="mono-bullet__marker" style={{ left: '100%' }} />
      </div>
      <div className="mono-business-chart__footer"><span>完成 {percent}%</span><span>目标为当前范围全部数据</span></div>
    </div>
  )
}

export function MonoTrendChart({
  data,
  series,
  label,
  height = 220,
}: {
  data: MonoTrendPoint[]
  series: MonoTrendSeries[]
  label: string
  height?: number
}) {
  return (
    <div className="mono-business-chart mono-business-chart--trend" aria-label={label}>
      <div className="mono-business-chart__header">
        <div>
          <span className="mono-business-chart__caption">{label}</span>
          <strong>{data.length} 个业务日</strong>
        </div>
        <div className="mono-business-chart__legend">
          {series.map(item => <span key={item.key}><i style={{ background: item.color }} />{item.label}</span>)}
        </div>
      </div>
      <div className="mono-business-chart__stage" style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 10, right: 10, bottom: 0, left: -16 }}>
            <XAxis dataKey="label" axisLine={false} tickLine={false} tick={{ fill: 'var(--app-text-muted)', fontSize: 10 }} />
            <YAxis axisLine={false} tickLine={false} width={42} tick={{ fill: 'var(--app-text-muted)', fontSize: 10 }} allowDecimals={false} />
            <Tooltip content={<MonoChartTooltip />} />
            {series.map(item => (
              <Area key={item.key} type="monotone" dataKey={item.key} name={item.label} stroke={item.color} fill={item.color} fillOpacity={0.09} strokeWidth={2} isAnimationActive={false} />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

export function MonoDonutChart({
  data,
  label,
  centerValue,
}: {
  data: MonoStatusDatum[]
  label: string
  centerValue: string | number
}) {
  const total = data.reduce((sum, item) => sum + Math.max(0, item.value), 0)
  return (
    <div className="mono-business-chart mono-business-chart--donut" aria-label={label}>
      <div className="mono-business-chart__header"><span className="mono-business-chart__caption">{label}</span><strong>{total.toLocaleString()} 条</strong></div>
      <div className="mono-donut__body">
        <div className="mono-donut__stage">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Tooltip content={<MonoChartTooltip />} />
              <Pie data={data} dataKey="value" nameKey="label" innerRadius="62%" outerRadius="86%" paddingAngle={3} stroke="none" isAnimationActive={false}>
                {data.map(item => <Cell key={item.label} fill={item.color} />)}
                <Label position="center" content={() => <text x="50%" y="49%" textAnchor="middle" dominantBaseline="middle" fill="var(--app-text-strong)" fontSize="20" fontWeight="700">{centerValue}</text>} />
              </Pie>
            </PieChart>
          </ResponsiveContainer>
        </div>
        <div className="mono-donut__legend">
          {data.map(item => <span key={item.label}><i style={{ background: item.color }} /><b>{item.label}</b><em>{item.value.toLocaleString()}</em></span>)}
        </div>
      </div>
    </div>
  )
}

export function MonoWaterfallChart({
  data,
  label,
}: {
  data: MonoStatusDatum[]
  label: string
}) {
  const maxValue = Math.max(...data.map(item => item.value), 1)
  return (
    <div className="mono-business-chart mono-business-chart--waterfall" aria-label={label}>
      <div className="mono-business-chart__header"><span className="mono-business-chart__caption">{label}</span><strong>结果构成</strong></div>
      <div className="mono-waterfall__stage">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 12, right: 8, bottom: 0, left: -16 }}>
            <XAxis dataKey="label" axisLine={false} tickLine={false} tick={{ fill: 'var(--app-text-muted)', fontSize: 10 }} />
            <YAxis domain={[0, maxValue]} axisLine={false} tickLine={false} allowDecimals={false} tick={{ fill: 'var(--app-text-muted)', fontSize: 10 }} />
            <ReferenceLine y={0} stroke="var(--app-border)" />
            <Tooltip content={<MonoChartTooltip />} />
            <Bar dataKey="value" name="数量" radius={[8, 8, 2, 2]} isAnimationActive={false}>
              {data.map(item => <Cell key={item.label} fill={item.color} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
