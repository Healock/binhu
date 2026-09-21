export type LeaveDateRange = [string, string]

/**
 * Keep a RangePicker's in-progress selection editable.
 * Selecting a new start must discard the previous end; otherwise an older end
 * can be carried into the next selection and make the start appear locked.
 */
export function updateLeaveDateRangeFromCalendar(
  dateStrings: readonly [string, string],
  range?: 'start' | 'end',
): LeaveDateRange {
  const start = dateStrings[0] || ''
  const end = dateStrings[1] || ''
  return range === 'start' ? [start, ''] : [start, end]
}

export function isCompleteLeaveDateRange(range: LeaveDateRange): boolean {
  return Boolean(range[0] && range[1] && range[1] >= range[0])
}
