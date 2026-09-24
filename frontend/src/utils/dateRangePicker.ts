export type DateRangePickerSelection = readonly [string, string]

/**
 * Keep the in-progress RangePicker selection visible while the user chooses
 * the second date. A new start begins a fresh range and must not retain the
 * previously committed end date.
 */
export function updateDateRangePickerSelection(
  dateStrings: DateRangePickerSelection,
  range?: 'start' | 'end',
): [string, string] {
  const start = dateStrings[0] || ''
  const end = dateStrings[1] || ''
  return range === 'start' ? [start, ''] : [start, end]
}

