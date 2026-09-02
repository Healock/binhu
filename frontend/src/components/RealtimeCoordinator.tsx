import { useEffect, useRef } from 'react'
import { useAuth } from '../context/AuthContext'
import { resolveRuntimeApiUrl } from '../utils/apiEnvironment.ts'

const EVENT_NAME = 'binhu:domain-event'

export default function RealtimeCoordinator() {
  const { user, environment } = useAuth()
  const sourceRef = useRef<EventSource | null>(null)
  const seenRef = useRef<string[]>([])
  const revisionsRef = useRef<Map<string, number>>(new Map())

  useEffect(() => {
    if (!user || typeof window === 'undefined' || typeof EventSource === 'undefined') return undefined
    const seen = seenRef.current
    const streamUrl = resolveRuntimeApiUrl('/api/events/stream')
    const source = new EventSource(streamUrl, { withCredentials: true })
    sourceRef.current = source
    const fallbackTimer = window.setInterval(() => {
      if (source.readyState !== EventSource.OPEN) {
        window.dispatchEvent(new CustomEvent('binhu:realtime-poll'))
      }
    }, 60_000)
    const handleEvent = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data || '{}') as Record<string, unknown>
        const eventId = String(payload.event_id || event.lastEventId || '')
        if (eventId && seen.includes(eventId)) return
        if (eventId) {
          seen.push(eventId)
          if (seen.length > 50000) seen.splice(0, 25000)
        }
        const aggregateKey = `${String(payload.aggregate_type || '')}:${String(payload.aggregate_id || '')}`
        const revision = Number(payload.aggregate_revision || 0)
        if (aggregateKey !== ':' && revision) {
          const previous = revisionsRef.current.get(aggregateKey) || 0
          if (revision < previous) return
          revisionsRef.current.set(aggregateKey, revision)
        }
        window.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: payload }))
      } catch {
        // Ignore malformed events; the next normal query remains authoritative.
      }
    }
    source.addEventListener('domain_event', handleEvent)
    source.addEventListener('resync_required', () => {
      window.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: { event_type: 'resync_required' } }))
    })
    return () => {
      source.close()
      window.clearInterval(fallbackTimer)
      sourceRef.current = null
    }
  }, [environment, user])

  return null
}

export { EVENT_NAME }
