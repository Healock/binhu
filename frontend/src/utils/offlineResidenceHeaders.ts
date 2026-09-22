const IDENTITY_HEADER_PRIORITY = [
  '公民身份号码',
  '居民身份证号',
  '身份证号码',
  '证件号码',
  '身份证号',
  '身份证',
] as const

const IDENTITY_HEADERS = new Map(IDENTITY_HEADER_PRIORITY.map((header, index) => [header, index]))
const NON_IDENTITY_HEADERS = new Set(['证件类型', '身份证件类型', '证件类别', '证件种类'])

function normalizeHeader(value: unknown): string {
  return String(value || '').replace(/[：:：\s]/gu, '')
}

export function identityHeaderScore(value: unknown): number {
  const header = normalizeHeader(value)
  if (NON_IDENTITY_HEADERS.has(header)) return -1
  const priority = IDENTITY_HEADERS.get(header)
  return priority === undefined ? -1 : IDENTITY_HEADER_PRIORITY.length - priority
}

/** Return the preferred identity-number column for one header row. */
export function selectIdentityColumn(headers: string[]): number {
  let candidate = -1
  let candidateScore = -1
  headers.forEach((value, column) => {
    const score = identityHeaderScore(value)
    if (score > candidateScore) {
      candidate = column
      candidateScore = score
    }
  })
  return candidate
}
