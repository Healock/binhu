import { useEffect, useMemo, useState } from 'react'
import {
  DownloadOutlined,
  LoadingOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import {
  resolveDesktopBridge,
  type DesktopUpdateState,
} from '../desktop/bridge'

function controlLabel(status: DesktopUpdateState | null) {
  switch (status?.state) {
    case 'checking': return '正在检查更新'
    case 'available': return `下载 ${status.availableVersion || '新版本'}`
    case 'downloading': return `下载中 ${status.progress ?? 0}%`
    case 'ready': return '重启并更新'
    case 'applying': return '正在应用更新'
    case 'error': return '重新检查更新'
    default: return '检查更新'
  }
}

export default function DesktopUpdateControl() {
  const bridge = useMemo(() => resolveDesktopBridge(), [])
  const [status, setStatus] = useState<DesktopUpdateState | null>(null)

  useEffect(() => {
    if (!bridge) return
    let mounted = true
    bridge.getUpdateStatus().then(value => {
      if (mounted && value) setStatus(value)
    }).catch(() => {})
    const unsubscribe = bridge.subscribeUpdateState(setStatus)
    return () => {
      mounted = false
      unsubscribe()
    }
  }, [bridge])

  if (!bridge) return null

  const busy = status?.state === 'checking'
    || status?.state === 'downloading'
    || status?.state === 'applying'
  const label = controlLabel(status)
  const title = status?.state === 'error' && status.error
    ? `${label}：${status.error}`
    : label

  const activate = () => {
    if (busy) return
    if (status?.state === 'available') {
      bridge.downloadUpdate().then(setStatus).catch(() => {})
    } else if (status?.state === 'ready') {
      bridge.restartAndApply().then(setStatus).catch(() => {})
    } else {
      bridge.checkForUpdates().then(setStatus).catch(() => {})
    }
  }

  const icon = (() => {
    if (status?.state === 'checking' || status?.state === 'downloading' || status?.state === 'applying') {
      return <LoadingOutlined spin />
    }
    if (status?.state === 'available') return <DownloadOutlined />
    if (status?.state === 'ready') return <PoweroffOutlined />
    if (status?.state === 'error') return <WarningOutlined />
    return <ReloadOutlined />
  })()

  const showText = status?.state === 'available'
    || status?.state === 'downloading'
    || status?.state === 'ready'
    || status?.state === 'applying'

  return (
    <button
      id="desktop-update-button"
      type="button"
      className={`desktop-titlebar__button desktop-update-control${showText ? ' desktop-update-control--wide' : ''}`}
      title={title}
      aria-label={title}
      disabled={busy}
      onClick={activate}
    >
      {icon}
      {showText && <span>{label}</span>}
    </button>
  )
}
