import { useEffect, useMemo, useState } from 'react'
import { DownloadOutlined, LoadingOutlined, PoweroffOutlined } from '@ant-design/icons'
import { useLocation, useNavigate } from 'react-router-dom'
import { resolveDesktopBridge, type DesktopUpdateState } from '../desktop/bridge'

export default function MandatoryUpdateGate() {
  const bridge = useMemo(() => resolveDesktopBridge(), [])
  const [status, setStatus] = useState<DesktopUpdateState | null>(null)
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    if (!bridge) return
    let mounted = true
    bridge.getUpdateStatus().then(value => { if (mounted) setStatus(value) }).catch(() => {})
    const unsubscribe = bridge.subscribeUpdateState(setStatus)
    return () => { mounted = false; unsubscribe() }
  }, [bridge])

  if (!bridge || !status?.mandatory || location.pathname === '/offline') return null

  const update = () => {
    if (status.state === 'available') bridge.downloadUpdate().then(setStatus).catch(() => {})
    else if (status.state === 'ready') bridge.restartAndApply().then(setStatus).catch(() => {})
    else bridge.checkForUpdates().then(setStatus).catch(() => {})
  }
  const busy = ['checking', 'downloading', 'applying'].includes(status.state)
  const label = status.state === 'available' ? '下载更新'
    : status.state === 'ready' ? '重启并更新'
      : status.state === 'downloading' ? `正在下载 ${status.progress ?? 0}%`
        : status.state === 'applying' ? '正在应用更新'
          : '重新检查'
  const icon = busy ? <LoadingOutlined spin />
    : status.state === 'ready' ? <PoweroffOutlined /> : <DownloadOutlined />

  return (
    <div className="mandatory-update" role="alertdialog" aria-modal="true" aria-labelledby="mandatory-update-title">
      <section className="mandatory-update__panel">
        <div className="mandatory-update__mark"><DownloadOutlined /></div>
        <h2 id="mandatory-update-title">需要更新桌面客户端</h2>
        <p>当前版本已停止在线业务访问。完成更新后可继续登录，离线工具仍可正常使用。</p>
        {status.error && <div className="mandatory-update__error">{status.error}</div>}
        <div className="mandatory-update__actions">
          <button type="button" className="mandatory-update__secondary" onClick={() => navigate('/offline')}>进入离线模式</button>
          <button type="button" className="mandatory-update__primary" disabled={busy} onClick={update}>{icon}<span>{label}</span></button>
        </div>
      </section>
    </div>
  )
}
