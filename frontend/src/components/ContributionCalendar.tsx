import HeatMap from '@uiw/react-heat-map'
import type { WorkContributionDay } from '../types'
import {
  contributionDateLabel,
  contributionDaysForYear,
} from '../utils/contributionCalendar'

interface ContributionCalendarProps {
  year: number
  days: WorkContributionDay[]
  className?: string
}

const PANEL_COLORS = {
  0: 'var(--contribution-0)',
  2: 'var(--contribution-1)',
  4: 'var(--contribution-2)',
  8: 'var(--contribution-3)',
  999999: 'var(--contribution-4)',
}

export default function ContributionCalendar({
  year,
  days,
  className = '',
}: ContributionCalendarProps) {
  const values = contributionDaysForYear(days, year)

  return (
    <div className={`contribution-calendar ${className}`.trim()}>
      <div className="contribution-calendar__scroll" tabIndex={0} aria-label={`${year} 年工作贡献日历`}>
        <HeatMap
          value={values}
          startDate={new Date(year, 0, 1)}
          endDate={new Date(year, 11, 31)}
          width={780}
          height={122}
          rectSize={10}
          space={3}
          legendCellSize={0}
          weekLabels={['日', '', '二', '', '四', '', '六']}
          monthLabels={['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']}
          panelColors={PANEL_COLORS}
          style={{
            minWidth: 780,
            color: 'var(--app-text-secondary)',
            '--rhm-text-color': 'var(--app-text-secondary)',
            '--rhm-rect-hover-stroke': 'var(--app-text-strong)',
          } as React.CSSProperties}
          rectProps={{ rx: 2, ry: 2 }}
          rectRender={(props, value) => (
            <rect
              {...props}
              role="img"
              aria-label={`${contributionDateLabel(value.date)}，${value.count || 0} 项工作`}
            >
              <title>{`${contributionDateLabel(value.date)}：${value.count || 0} 项工作`}</title>
            </rect>
          )}
        />
      </div>
      <div className="contribution-calendar__legend" aria-label="贡献强度图例">
        <span>少</span>
        {[0, 1, 2, 3, 4].map(level => (
          <span
            key={level}
            className="contribution-calendar__legend-cell"
            style={{ background: `var(--contribution-${level})` }}
          />
        ))}
        <span>多</span>
      </div>
    </div>
  )
}
