import { useCallback, useEffect, useRef, useState } from 'react'
import type { SyncStatus } from '../types'
import { getSyncStatus, triggerSync } from '../api/client'

const ACTIVE_STATUSES = new Set(['pending', 'running'])
const TERMINAL_STATUSES = new Set(['success', 'completed', 'partial', 'failed'])

export function useSync(onComplete?: () => void) {
  const [status, setStatus] = useState<SyncStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const latestRef = useRef<SyncStatus | null>(null)
  const completeRef = useRef(onComplete)

  useEffect(() => {
    completeRef.current = onComplete
  }, [onComplete])

  const refresh = useCallback(async () => {
    try {
      const next = await getSyncStatus()
      const previous = latestRef.current
      latestRef.current = next
      setStatus(next)

      if (next.status === 'failed' || next.status === 'partial') {
        setError(next.error_message || (
          next.status === 'partial' ? '部分数据同步失败' : '同步失败'
        ))
      } else {
        setError(null)
      }

      const isNewTerminalTask = Boolean(
        previous
        && TERMINAL_STATUSES.has(next.status)
        && (
          previous.task_id !== next.task_id
          || ACTIVE_STATUSES.has(previous.status)
        )
      )
      if (isNewTerminalTask) completeRef.current?.()
      return next
    } catch {
      setError('无法获取同步状态，请稍后重试')
      return null
    }
  }, [])

  useEffect(() => {
    let stopped = false
    let timer: number | null = null

    const poll = async () => {
      const next = await refresh()
      if (stopped) return
      const active = next && ACTIVE_STATUSES.has(next.status)
      timer = window.setTimeout(poll, active ? 2000 : 10000)
    }

    void poll()
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    document.addEventListener('visibilitychange', handleVisibility)

    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [refresh])

  const startSync = useCallback(async () => {
    setError(null)
    try {
      const result = await triggerSync()
      if (result.status === 'conflict') setError(result.message)
      await refresh()
    } catch (e: any) {
      setError(e?.response?.data?.detail || '触发同步失败')
    }
  }, [refresh])

  const syncing = Boolean(
    status && ACTIVE_STATUSES.has(status.status),
  )

  return {
    syncing,
    status,
    error,
    startSync,
    refresh,
  }
}
