import {
  DownloadOutlined,
  LoadingOutlined,
  PoweroffOutlined,
} from '@ant-design/icons'
import { useDesktopUpdateStatus } from '../desktop/useDesktopUpdateStatus'

export default function DesktopUpdateControl() {
  const { bridge, status, setStatus } = useDesktopUpdateStatus()

  if (!bridge || !status || !['available', 'downloading', 'ready', 'applying'].includes(status.state)) {
    return null
  }

  const busy = status?.state === 'checking'
    || status?.state === 'downloading'
    || status?.state === 'applying'
  const label = status.state === 'available'
    ? `下载新版本${status.availableVersion ? ` v${status.availableVersion}` : ''}`
    : status.state === 'downloading'
      ? `正在下载${status.progress == null ? '' : ` ${status.progress}%`}`
      : status.state === 'ready'
        ? `新版本${status.availableVersion ? ` v${status.availableVersion}` : ''}已下载，点击重启`
        : '正在应用更新'
  const title = label

  const activate = () => {
    if (busy) return
    if (status?.state === 'available') {
      bridge.downloadUpdate().then(setStatus).catch(() => {})
    } else if (status?.state === 'ready') {
      bridge.restartAndApply().then(setStatus).catch(() => {})
    }
  }

  const icon = (() => {
    if (status?.state === 'checking' || status?.state === 'downloading' || status?.state === 'applying') {
      return <LoadingOutlined spin />
    }
    if (status?.state === 'available') return <DownloadOutlined />
    if (status?.state === 'ready') return <PoweroffOutlined />
    return <DownloadOutlined />
  })()

  return (
    <button
      id="desktop-update-button"
      type="button"
      className="desktop-titlebar__button desktop-update-control"
      title={title}
      aria-label={title}
      disabled={busy}
      onClick={activate}
    >
      {icon}
    </button>
  )
}
