import { useEffect, useState } from 'react'
import { CheckCircleOutlined, CloseOutlined } from '@ant-design/icons'
import { useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { resolveDesktopBridge, type DesktopBridge, type DesktopUpgradeInfo } from '../desktop/bridge'

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
  const [bridge, setBridge] = useState<DesktopBridge | null>(null)
  const { user, loading } = useAuth()
  const location = useLocation()
  const [upgrade, setUpgrade] = useState<DesktopUpgradeInfo | null>(null)
  const [notes, setNotes] = useState<ReleaseNotes | null>(null)

  useEffect(() => {
    let disposed = false
    let attempts = 0
    const resolve = () => {
      if (disposed) return
      const value = resolveDesktopBridge()
      if (value) {
        setBridge(value)
        return
      }
      attempts += 1
      if (attempts < 40) window.setTimeout(resolve, 50)
    }
    resolve()
    return () => { disposed = true }
  }, [])

  useEffect(() => {
    if (!bridge) return
    let mounted = true
    bridge.getUpgradeInfo()
      .then(value => { if (mounted) setUpgrade(value) })
      .catch(() => {})
    return () => { mounted = false }
  }, [bridge])

  useEffect(() => {
    let mounted = true
    fetch('/release-notes.json', { cache: 'no-store' })
      .then(response => response.ok ? response.json() : null)
      .then(value => {
        if (mounted && value?.version === (upgrade?.currentVersion || __APP_VERSION__)) setNotes(value)
      })
      .catch(() => {})
    return () => { mounted = false }
  }, [upgrade?.currentVersion])

  const currentVersion = upgrade?.currentVersion || __APP_VERSION__
  const acknowledgedVersion = (() => {
    try { return window.localStorage.getItem('binhu.version-updated.acknowledged') } catch (_error) { return null }
  })()
  const notesFallback = !upgrade
    && Boolean(notes?.previousVersion && notes.previousVersion !== currentVersion && acknowledgedVersion !== currentVersion)
  const detectedUpgrade = Boolean(
    upgrade?.upgradedFrom
    || upgrade?.upgradeDetected
    || notesFallback,
  )
  const upgradedFrom = upgrade?.upgradedFrom || notes?.previousVersion || null

  if (
    !bridge
    || loading
    || !user
    || !detectedUpgrade
    || location.pathname === '/login'
    || location.pathname === '/offline'
  ) return null

  const close = () => {
    try { window.localStorage.setItem('binhu.version-updated.acknowledged', currentVersion) } catch (_error) {}
    bridge.acknowledgeUpgrade()
      .then(setUpgrade)
      .catch(() => setUpgrade(current => current
        ? { ...current, upgradedFrom: null, upgradeDetected: false }
        : null))
  }

  return (
    <div className="version-updated" role="dialog" aria-modal="true" aria-labelledby="version-updated-title">
      <section className="version-updated__panel">
        <button type="button" className="version-updated__close" aria-label="关闭更新说明" onClick={close}>
          <CloseOutlined />
        </button>
        <div className="version-updated__mark"><CheckCircleOutlined /></div>
        <h2 id="version-updated-title">版本已更新</h2>
        <p className="version-updated__versions">v{upgradedFrom || '上一版本'} <span>→</span> v{currentVersion}</p>
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
          <p className="version-updated__empty">更新日志暂时无法加载，请稍后在版本信息中查看。</p>
        )}
        <button type="button" className="version-updated__confirm" onClick={close}>知道了</button>
      </section>
    </div>
  )
}
