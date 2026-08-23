export interface ReleaseNotePullRequest {
  number: number
  title: string
  summary: string
}

export interface ReleaseNoteSection {
  title: string
  items: string[]
  pullRequests?: number[]
}

export interface ReleaseNotes {
  schemaVersion?: number
  version: string
  previousVersion: string | null
  pullRequests: ReleaseNotePullRequest[]
  sections?: ReleaseNoteSection[]
  commit?: string
}

function isPullRequest(value: unknown): value is ReleaseNotePullRequest {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return Number.isInteger(item.number)
    && typeof item.title === 'string'
    && typeof item.summary === 'string'
}

function isSection(value: unknown): value is ReleaseNoteSection {
  if (!value || typeof value !== 'object') return false
  const section = value as Record<string, unknown>
  return typeof section.title === 'string'
    && Array.isArray(section.items)
    && section.items.every(item => typeof item === 'string')
    && (section.pullRequests === undefined
      || (Array.isArray(section.pullRequests) && section.pullRequests.every(item => Number.isInteger(item))))
}

export function isReleaseNotes(value: unknown): value is ReleaseNotes {
  if (!value || typeof value !== 'object') return false
  const notes = value as Record<string, unknown>
  return typeof notes.version === 'string'
    && (notes.previousVersion === null || typeof notes.previousVersion === 'string')
    && Array.isArray(notes.pullRequests)
    && notes.pullRequests.every(isPullRequest)
    && (notes.sections === undefined || (Array.isArray(notes.sections) && notes.sections.every(isSection)))
}

export function releaseNotesCandidates(locationHref?: string): string[] {
  const candidates: string[] = []
  const add = (value: string) => {
    if (!candidates.includes(value)) candidates.push(value)
  }

  if (locationHref) {
    try {
      add(new URL('release-notes.json', locationHref).toString())
    } catch (_error) {
      // Fall through to the relative candidates below.
    }
  }
  add('release-notes.json')
  add('./release-notes.json')
  add('/release-notes.json')
  return candidates
}

export async function loadReleaseNotes(
  currentVersion: string,
  fetcher: typeof fetch = globalThis.fetch,
  locationHref = typeof window === 'undefined' ? undefined : window.location.href,
): Promise<ReleaseNotes | null> {
  for (const candidate of releaseNotesCandidates(locationHref)) {
    try {
      const response = await fetcher(candidate, { cache: 'no-store' })
      if (!response.ok) continue
      const value: unknown = await response.json()
      if (isReleaseNotes(value) && value.version === currentVersion) return value
    } catch (_error) {
      // A desktop protocol can reject one URL form while serving another.
    }
  }
  return null
}
