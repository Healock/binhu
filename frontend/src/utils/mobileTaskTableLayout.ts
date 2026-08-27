export type CompactPersonnelLayout = 'paired' | 'stacked'

export interface CompactPersonnelPresentation {
  layout: CompactPersonnelLayout
  columnWidth: number
  tableScrollWidth: number
}

const PAIRED_PERSONNEL_MIN_WIDTH = 900

export function getCompactPersonnelPresentation(containerWidth: number): CompactPersonnelPresentation {
  if (containerWidth >= PAIRED_PERSONNEL_MIN_WIDTH) {
    return {
      layout: 'paired',
      columnWidth: 520,
      tableScrollWidth: 1020,
    }
  }

  return {
    layout: 'stacked',
    columnWidth: 360,
    tableScrollWidth: 900,
  }
}
