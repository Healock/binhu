import { useEffect, useMemo, useState } from 'react'
import {
  resolveClientUpdateBridge,
  type ClientUpdateBridge,
  type ClientUpdateState,
} from './bridge'

export function useClientUpdateStatus(): {
  bridge: ClientUpdateBridge | null
  status: ClientUpdateState | null
  setStatus: (status: ClientUpdateState) => void
} {
  const bridge = useMemo(() => resolveClientUpdateBridge(), [])
  const [status, setStatus] = useState<ClientUpdateState | null>(null)

  useEffect(() => {
    if (!bridge) return
    let mounted = true
    bridge.getUpdateStatus()
      .then(value => {
        if (mounted && value) setStatus(value)
      })
      .catch(() => {})
    const unsubscribe = bridge.subscribeUpdateState(value => {
      if (mounted) setStatus(value)
    })
    return () => {
      mounted = false
      unsubscribe()
    }
  }, [bridge])

  return { bridge, status, setStatus }
}
