import { useEffect, useMemo } from 'react'
import { resolveClientUpdateBridge } from '../desktop/bridge'

const INITIAL_CHECK_DELAY_MS = 15_000
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000

export default function ClientUpdateCoordinator() {
  const bridge = useMemo(() => resolveClientUpdateBridge(), [])

  useEffect(() => {
    if (!bridge) return
    let lastCheckAt = Date.now()
    let checking = false

    const check = async () => {
      if (checking) return
      checking = true
      lastCheckAt = Date.now()
      try {
        await bridge.checkForUpdates()
      } catch {
        // Update checks must never interrupt normal or offline work.
      } finally {
        checking = false
      }
    }

    const initialTimer = window.setTimeout(() => void check(), INITIAL_CHECK_DELAY_MS)
    const intervalTimer = window.setInterval(() => void check(), CHECK_INTERVAL_MS)
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible' && Date.now() - lastCheckAt >= CHECK_INTERVAL_MS) {
        void check()
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      window.clearTimeout(initialTimer)
      window.clearInterval(intervalTimer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [bridge])

  return null
}
