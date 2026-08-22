import { useEffect, useMemo, useState } from 'react'
import { CheckCircleOutlined, CloseOutlined } from '@ant-design/icons'
import { useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { resolveDesktopBridge, type DesktopUpgradeInfo } from '../desktop/bridge'

interface ReleaseNotePullRequest {
  number: number
  title: string
  summary: string
}

interface ReleaseNotes {
  version: string
  previousVersion: string | null
  pullRequests: ReleaseNotePullRequest[]
}

export default function VersionUpdatedGate() {
  const bridge = useMemo(() => resolveDesktopBridge(), [])
  const { user, loading } = useAuth()
  const location = useLocation()
  const [upgrade, setUpgrade] = useState<DesktopUpgradeInfo | null>(null)
  const [notes, setNotes] = useState<ReleaseNotes | null>(null)

  useEffect(() => {
    if (!bridge) return
    let mounted = true
    bridge.getUpgradeInfo()
      .then(value => { if (mounted) setUpgrade(value) })
      .catch(() => {})
    return () => { mounted = false }
  }, [bridge])

  useEffect(() => {
    if (!upgrade?.upgradedFrom) return
    let mounted = true
    fetch('/release-notes.json', { cache: 'no-store' })
      .then(response => response.ok ? response.json() : null)
      .then(value => { if (mounted && value?.version === upgrade.currentVersion) setNotes(value) })
      .catch(() => {})
    return () => { mounted = false }
  }, [upgrade])

  if (
    !bridge
    || loading
    || !user
    || !upgrade?.upgradedFrom
    || location.pathname === '/login'
    || location.pathname === '/offline'
  ) return null

  const close = () => {
    bridge.acknowledgeUpgrade().then(setUpgrade).catch(() => setUpgrade({ ...upgrade, upgradedFrom: null }))
  }

  return (
    <div className="version-updated" role="dialog" aria-modal="true" aria-labelledby="version-updated-title">
      <section className="version-updated__panel">
        <button type="button" className="version-updated__close" aria-label="关闭更新说明" onClick={close}>
          <CloseOutlined />
        </button>
        <div className="version-updated__mark"><CheckCircleOutlined /></div>
        <h2 id="version-updated-title">版本已更新</h2>
        <p className="version-updated__versions">v{upgrade.upgradedFrom} <span>→</span> v{upgrade.currentVersion}</p>
        {notes?.pullRequests?.length ? (
          <div className="version-updated__notes">
            <h3>本次更新内容</h3>
            <ul>
              {notes.pullRequests.map(item => (
                <li key={item.number}>
                  <strong>#{item.number} {item.title}</strong>
                  <span>{item.summary}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="version-updated__empty">本次版本暂无详细更新说明。</p>
        )}
        <button type="button" className="version-updated__confirm" onClick={close}>知道了</button>
      </section>
    </div>
  )
}
