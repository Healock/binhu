import type { WorkLogColumn } from '../types'

type Values = Record<string, unknown>
type Row = Record<string, unknown>

function number(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function sum(rows: Row[], key: string): number {
  return rows.reduce((total, row) => total + number(row[key]), 0)
}

function rounded(value: number): number {
  return Math.round((value + Number.EPSILON) * 10) / 10
}

function ratio(numerator: number, denominator: number): number | null {
  return denominator > 0 ? rounded(numerator * 100 / denominator) : null
}

export function leafWorkLogColumns(columns: WorkLogColumn[]): WorkLogColumn[] {
  return columns.flatMap(column => (
    column.children?.length
      ? leafWorkLogColumns(column.children)
      : [column]
  ))
}

export function deriveWorkLogValues(values: Values): Values {
  const derived: Values = {}
  const instruction = Array.isArray(values['flow.instruction_table'])
    ? values['flow.instruction_table'] as Row[]
    : []
  if (instruction.length > 0) {
    const total = sum(instruction, 'total')
    const unchecked = sum(instruction, 'unchecked')
    const checked = sum(instruction, 'checked')
    const unable = sum(instruction, 'unable')
    const members = sum(instruction, 'grid_member_count')
    Object.assign(derived, {
      'flow.instruction.total': total,
      'flow.instruction.unchecked': unchecked,
      'flow.instruction.checked': checked,
      'flow.instruction.unable': unable,
      'flow.instruction.completion_rate': ratio(checked, total),
      'flow.instruction.ground_rate': ratio(
        Math.max(checked - unable, 0),
        checked,
      ),
      'flow.instruction.average_checked': members > 0
        ? rounded(checked / members)
        : null,
    })
  }

  const rental = Array.isArray(values['rental.visit_table'])
    ? values['rental.visit_table'] as Row[]
    : []
  if (rental.length > 0) {
    const visits = sum(rental, 'visits')
    const added = sum(rental, 'added')
    const changed = sum(rental, 'changed')
    const cancelled = sum(rental, 'cancelled')
    const members = sum(rental, 'grid_member_count')
    const rated = sum(rental, 'rated')
    const changes = added + changed + cancelled
    Object.assign(derived, {
      'rental.visit.visits': visits,
      'rental.visit.added': added,
      'rental.visit.changed': changed,
      'rental.visit.cancelled': cancelled,
      'rental.visit.average_visits': members > 0
        ? rounded(visits / members)
        : null,
      'rental.visit.average_changes': members > 0
        ? rounded(changes / members)
        : null,
      'rental.visit.household_changes': visits > 0
        ? rounded(changes / visits)
        : null,
      'rental.visit.rated': rated,
      'rental.visit.rating_rate': ratio(rated, visits),
    })
  }
  return derived
}
