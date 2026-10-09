import { retryDelay } from './resilientPolling.ts'

export interface EventSourceLike {
  onopen: ((event: Event) => void) | null
  onerror: ((event: Event) => void) | null
  addEventListener(type: string, listener: EventListener): void
  close(): void
}

export function appendEventCursor(url: string, lastEventId: string): string {
  if (!lastEventId) return url
  const target = new URL(url, window.location.origin)
  target.searchParams.set('last_event_id', lastEventId)
  return target.toString()
}

export function connectResilientEventSource(
  url: string,
  configure: (source: EventSourceLike) => void,
  options: {
    factory?: (url: string) => EventSourceLike
    random?: () => number
    onState?: (state: 'connecting' | 'connected' | 'disconnected') => void
    onReconnect?: (details: { attempt: number; delay_ms: number }) => void
  } = {},
) {
  const factory = options.factory ?? ((target) => new EventSource(target, { withCredentials: true }))
  let source: EventSourceLike | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let stopped = false
  let failures = 0
  let lastEventId = ''

  const connect = () => {
    if (stopped || document.visibilityState === 'hidden') return
    options.onState?.('connecting')
    const current = factory(appendEventCursor(url, lastEventId))
    let openedAt: number | null = null
    source = current
    configure(current)
    current.onopen = () => {
      if (source !== current || stopped) return
      openedAt = Date.now()
      options.onState?.('connected')
    }
    current.onerror = () => {
      if (source !== current || stopped) return
      current.close()
      source = null
      // A 200 followed by an immediate close must not reset the retry budget.
      if (openedAt !== null && Date.now() - openedAt >= 30_000) failures = 0
      failures += 1
      options.onState?.('disconnected')
      const delay = failures >= 6
        ? 120_000
        : retryDelay(1000, failures, 30_000, 0.25, options.random ?? Math.random)
      options.onReconnect?.({ attempt: failures, delay_ms: delay })
      timer = setTimeout(connect, delay)
    }
  }

  connect()
  const resume = () => {
    if (stopped || document.visibilityState === 'hidden' || source) return
    if (timer) clearTimeout(timer)
    timer = setTimeout(connect, Math.round(250 + (options.random ?? Math.random)() * 750))
  }
  window.addEventListener('online', resume)
  document.addEventListener('visibilitychange', resume)
  return {
    setLastEventId(value: string) { if (value) lastEventId = value },
    close() {
      stopped = true
      if (timer) clearTimeout(timer)
      source?.close()
      source = null
      window.removeEventListener('online', resume)
      document.removeEventListener('visibilitychange', resume)
    },
  }
}
