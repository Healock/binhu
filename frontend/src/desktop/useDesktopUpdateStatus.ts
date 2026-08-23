import { useEffect, useMemo, useState } from 'react'
import {
  resolveDesktopBridge,
  type DesktopBridge,
  type DesktopUpdateState,
} from './bridge'

export function useDesktopUpdateStatus(): {
  bridge: DesktopBridge | null
  status: DesktopUpdateState | null
  setStatus: (status: DesktopUpdateState) => void
} {
  const bridge = useMemo(() => resolveDesktopBridge(), [])
  const [status, setStatus] = useState<DesktopUpdateState | null>(null)

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
