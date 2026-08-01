export const PERSONNEL_POSITIONS = [
  '中队长',
  '基础管控',
  '自购房',
  '片长',
  '组长',
  '组员',
  '社区民警',
  '所队领导',
] as const

export type PersonnelPosition = typeof PERSONNEL_POSITIONS[number]

export const DEFAULT_SUMMARY_POSITIONS: PersonnelPosition[] = ['组长', '组员']
export const RENTAL_PERSONNEL_POSITIONS = PERSONNEL_POSITIONS.filter(
  position => position !== '自购房',
)

export function parseSummaryPositions(value: unknown): PersonnelPosition[] {
  if (typeof value !== 'string' || !value) {
    return [...DEFAULT_SUMMARY_POSITIONS]
  }
  try {
    const parsed = JSON.parse(value)
    if (!Array.isArray(parsed)) return [...DEFAULT_SUMMARY_POSITIONS]
    const positions = parsed.filter(
      (item): item is PersonnelPosition => (
        PERSONNEL_POSITIONS.includes(item as PersonnelPosition)
      ),
    )
    return positions.length ? Array.from(new Set(positions)) : [...DEFAULT_SUMMARY_POSITIONS]
  } catch {
    return [...DEFAULT_SUMMARY_POSITIONS]
  }
}
