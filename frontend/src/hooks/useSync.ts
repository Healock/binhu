import { useCallback, useEffect, useRef, useState } from 'react'
import type { SyncStatus } from '../types'
import { getSyncStatus, triggerSync } from '../api/client'

const ACTIVE_STATUSES = new Set(['pending', 'running'])
const TERMINAL_STATUSES = new Set(['success', 'completed', 'partial', 'failed'])

export function useSync(onComplete?: () => void) {
  const [status, setStatus] = useState<SyncStatus | null>(null)
  const [taskError, setTaskError] = useState<string | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
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
      setStatusError(null)

      if (next.status === 'failed' || next.status === 'partial') {
        setTaskError(next.error_message || (
          next.status === 'partial' ? '部分数据同步失败' : '同步失败'
        ))
      } else {
        setTaskError(null)
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
    } catch (error: any) {
      const detail = error?.response?.data?.detail
      const reason = typeof detail === 'string'
        ? detail
        : detail?.message || error?.message || '网络连接异常'
      setStatusError(`无法获取同步状态：${reason}`)
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
    setActionError(null)
    try {
      const result = await triggerSync()
      if (result.status === 'conflict') setActionError(result.message)
      await refresh()
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      setActionError(
        typeof detail === 'string'
          ? detail
          : detail?.message || e?.message || '触发同步失败',
      )
    }
  }, [refresh])

  const syncing = Boolean(
    status && ACTIVE_STATUSES.has(status.status),
  )

  return {
    syncing,
    status,
    taskError,
    statusError,
    actionError,
    startSync,
    refresh,
  }
}
