import { useState, useCallback, useRef } from 'react'
import type { SyncStatus } from '../types'
import { triggerSync, getSyncStatus } from '../api/client'

export function useSync(onComplete?: () => void) {
  const [syncing, setSyncing] = useState(false)
  const [status, setStatus] = useState<SyncStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const startSync = useCallback(async () => {
    setSyncing(true)
    setError(null)
    try {
      const res = await triggerSync()
      if (res.status === 'conflict') {
        setError(res.message)
        setSyncing(false)
        return
      }
      startPolling()
    } catch (e: any) {
      setError(e?.response?.data?.detail || '触发同步失败')
      setSyncing(false)
    }
  }, [])

  const startPolling = useCallback(() => {
    const check = async () => {
      try {
        const s = await getSyncStatus()
        setStatus(s)
        if (s.status === 'success') {
          stopPolling()
          setSyncing(false)
          onComplete?.()
        } else if (s.status === 'failed') {
          stopPolling()
          setSyncing(false)
          setError(s.error_message || '同步失败')
        }
      } catch {
        // polling error, continue
      }
    }
    pollRef.current = setInterval(check, 2000)
    check()
  }, [onComplete])

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  return { syncing, status, error, startSync }
}
