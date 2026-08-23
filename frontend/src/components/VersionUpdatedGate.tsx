import { useEffect, useState } from 'react'
import { CheckCircleOutlined, NotificationOutlined } from '@ant-design/icons'
import { useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import {
  getImportantUnreadAnnouncements,
  markNotificationRead,
} from '../api/client'
import { resolveDesktopBridge, type DesktopBridge, type DesktopUpgradeInfo } from '../desktop/bridge'
import type { AppNotification } from '../types'
import { loadReleaseNotes, type ReleaseNotes } from '../utils/releaseNotes'
import FullscreenNoticeDialog from './FullscreenNoticeDialog'

export default function VersionUpdatedGate() {
  const [bridge, setBridge] = useState<DesktopBridge | null>(null)
  const { user, loading } = useAuth()
  const location = useLocation()
  const [upgrade, setUpgrade] = useState<DesktopUpgradeInfo | null>(null)
  const [notes, setNotes] = useState<ReleaseNotes | null>(null)
  const [announcements, setAnnouncements] = useState<AppNotification[]>([])
  const [announcementLoading, setAnnouncementLoading] = useState(false)
  const [announcementError, setAnnouncementError] = useState('')
  const currentVersion = upgrade?.currentVersion || __APP_VERSION__

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
    setNotes(null)
    loadReleaseNotes(currentVersion)
      .then(value => { if (mounted) setNotes(value) })
    return () => { mounted = false }
  }, [currentVersion])

  useEffect(() => {
    if (loading || !user || location.pathname === '/login' || location.pathname === '/offline') {
      setAnnouncements([])
      return
    }
    let mounted = true
    getImportantUnreadAnnouncements()
      .then(items => {
        if (mounted) setAnnouncements(items)
      })
      .catch(() => {
        // 公告读取失败不能阻断用户进入平台；消息中心仍可在稍后重新读取。
      })
    return () => { mounted = false }
  }, [loading, location.pathname, user?.id])

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

  if (loading || !user || location.pathname === '/login' || location.pathname === '/offline') return null

  const close = () => {
    try { window.localStorage.setItem('binhu.version-updated.acknowledged', currentVersion) } catch (_error) {}
    bridge.acknowledgeUpgrade()
      .then(setUpgrade)
      .catch(() => setUpgrade(current => current
        ? { ...current, upgradedFrom: null, upgradeDetected: false }
        : null))
  }

  if (bridge && detectedUpgrade) {
    return (
      <FullscreenNoticeDialog
        title="版本已更新"
        titleId="version-updated-title"
        mark={<CheckCircleOutlined />}
        subtitle={<span>v{upgradedFrom || '上一版本'} <b>→</b> v{currentVersion}</span>}
        closeLabel="关闭更新说明"
        onConfirm={close}
      >
        {notes?.sections?.length ? (
          <div className="fullscreen-notice__notes">
            <h3>本次更新内容</h3>
            <div className="fullscreen-notice__sections">
              {notes.sections.map((section, index) => (
                <section key={`${section.title}-${index}`} className="fullscreen-notice__section">
                  <h4>{index + 1}. {section.title}</h4>
                  <ul>
                    {section.items.map((item, itemIndex) => <li key={`${section.title}-${itemIndex}`}>{item}</li>)}
                  </ul>
                </section>
              ))}
            </div>
          </div>
        ) : notes?.pullRequests?.length ? (
          <div className="fullscreen-notice__notes">
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
          <p className="fullscreen-notice__empty">更新日志暂时无法加载，请稍后在版本信息中查看。</p>
        )}
      </FullscreenNoticeDialog>
    )
  }

  const announcement = announcements[0]
  if (!announcement) return null

  const acknowledgeAnnouncement = async () => {
    if (announcementLoading) return
    setAnnouncementLoading(true)
    setAnnouncementError('')
    try {
      await markNotificationRead(announcement)
      setAnnouncements(current => current.filter(item => item.id !== announcement.id))
      window.dispatchEvent(new Event('binhu:notifications-changed'))
    } catch {
      setAnnouncementError('阅读状态保存失败，请检查网络后重试。')
    } finally {
      setAnnouncementLoading(false)
    }
  }

  return (
    <FullscreenNoticeDialog
      title={announcement.title}
      titleId={`important-announcement-${announcement.id}`}
      mark={<NotificationOutlined />}
      subtitle={<span>重要公告 · 登录后须确认阅读</span>}
      confirmText="我已阅读"
      confirmLoading={announcementLoading}
      onConfirm={() => void acknowledgeAnnouncement()}
    >
      <div className="fullscreen-notice__announcement">{announcement.content}</div>
      {announcements.length > 1 && (
        <div className="fullscreen-notice__queue-hint">
          确认后还有 {announcements.length - 1} 条重要公告
        </div>
      )}
      {announcementError && <div className="fullscreen-notice__error">{announcementError}</div>}
    </FullscreenNoticeDialog>
  )
}
