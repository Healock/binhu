import type { WorkContributionDay } from '../types'

export function contributionLevel(count: number): 0 | 1 | 2 | 3 | 4 {
  if (count <= 0) return 0
  if (count === 1) return 1
  if (count <= 3) return 2
  if (count <= 7) return 3
  return 4
}

export function contributionDaysForYear(
  days: WorkContributionDay[],
  year: number,
): WorkContributionDay[] {
  const prefix = `${year}-`
  return days.filter(item => item.date.startsWith(prefix) && item.count > 0)
}

export function contributionDateLabel(value: string): string {
  const [year, month, day] = value.split(/[/-]/).map(Number)
  if (!year || !month || !day) return value
  return `${year}年${month}月${day}日`
}
