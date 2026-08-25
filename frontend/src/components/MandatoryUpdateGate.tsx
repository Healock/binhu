import { type ReactNode } from 'react'
import { DownloadOutlined, LoadingOutlined, PoweroffOutlined } from '@ant-design/icons'
import { useLocation, useNavigate } from 'react-router-dom'
import { isAndroidClientRuntime } from '../desktop/bridge'
import { useClientUpdateStatus } from '../desktop/useClientUpdateStatus'

export default function MandatoryUpdateGate({ children }: { children: ReactNode }) {
  const { bridge, status, setStatus } = useClientUpdateStatus()
  const location = useLocation()
  const navigate = useNavigate()
  const android = status?.platform === 'android'

  if (bridge && isAndroidClientRuntime() && !status) {
    return (
      <div className="mandatory-update mandatory-update--android" role="status">
        <section className="mandatory-update__panel">
          <div className="mandatory-update__mark"><LoadingOutlined spin /></div>
          <h2>正在检查客户端状态</h2>
          <p>正在读取本机更新策略，请稍候。</p>
        </section>
      </div>
    )
  }

  if (!bridge || !status?.mandatory || (!android && location.pathname === '/offline')) return children

  const update = () => {
    if (status.state === 'available') bridge.downloadUpdate().then(setStatus).catch(() => {})
    else if (status.state === 'ready') bridge.restartAndApply().then(setStatus).catch(() => {})
    else bridge.checkForUpdates().then(setStatus).catch(() => {})
  }
  const busy = ['checking', 'downloading', 'applying'].includes(status.state)
  const label = status.state === 'available' ? '下载更新'
    : status.state === 'ready' ? android ? '安装更新' : '重启并更新'
      : status.state === 'downloading' ? `正在下载 ${status.progress ?? 0}%`
        : status.state === 'applying' ? android ? '正在打开安装界面' : '正在应用更新'
          : '重新检查'
  const icon = busy ? <LoadingOutlined spin />
    : status.state === 'ready' ? <PoweroffOutlined /> : <DownloadOutlined />

  return (
    <div className={`mandatory-update${android ? ' mandatory-update--android' : ''}`} role="alertdialog" aria-modal="true" aria-labelledby="mandatory-update-title">
      <section className="mandatory-update__panel">
        <div className="mandatory-update__mark"><DownloadOutlined /></div>
        <h2 id="mandatory-update-title">需要更新{android ? ' Android 客户端' : '桌面客户端'}</h2>
        <p>{android
          ? '当前版本已停止使用。请完成更新后继续登录或使用离线工具。'
          : '当前版本已停止在线业务访问。完成更新后可继续登录，离线工具仍可正常使用。'}</p>
        {status.error && <div className="mandatory-update__error">{status.error}</div>}
        <div className="mandatory-update__actions">
          {!android && <button type="button" className="mandatory-update__secondary" onClick={() => navigate('/offline')}>进入离线模式</button>}
          <button type="button" className="mandatory-update__primary" disabled={busy} onClick={update}>{icon}<span>{label}</span></button>
        </div>
      </section>
    </div>
  )
}
