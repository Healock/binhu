import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function SessionTimeoutGuard() {
  const { user, recordActivity, refreshUser, logout } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [remaining, setRemaining] = useState<number | null>(null)
  const initialRoute = useRef(true)
  const lastActivityReportAt = useRef(0)
  const reportingActivity = useRef(false)

  const deadlineInfo = useMemo(() => {
    if (!user?.session_policy) return null
    const idleDeadline = new Date(user.session_policy.last_activity_at).getTime()
      + user.session_policy.idle_timeout_minutes * 60_000
    const absoluteDeadline = new Date(
      user.session_policy.absolute_expires_at,
    ).getTime()
    return idleDeadline <= absoluteDeadline
      ? {
          deadline: idleDeadline,
          code: 'session_idle_timeout',
          message: '登录已到期，请重新登录',
        }
      : {
          deadline: absoluteDeadline,
          code: 'session_expired',
          message: '登录已到期，请重新登录',
        }
  }, [user])
  const serverOffset = useMemo(() => (
    user?.session_policy
      ? new Date(user.session_policy.server_time).getTime() - Date.now()
      : 0
  ), [user])

  const reportRealActivity = (force = false) => {
    if (!user || reportingActivity.current) return
    const now = Date.now()
    if (!force && now - lastActivityReportAt.current < 60_000) return
    lastActivityReportAt.current = now
    reportingActivity.current = true
    recordActivity()
      .catch(() => {})
      .finally(() => { reportingActivity.current = false })
  }

  useEffect(() => {
    if (!user) return
    const onActivity = () => reportRealActivity()
    const events: Array<keyof WindowEventMap> = ['click', 'touchstart', 'keydown', 'wheel']
    events.forEach(eventName => window.addEventListener(eventName, onActivity, { passive: true }))
    return () => {
      events.forEach(eventName => window.removeEventListener(eventName, onActivity))
    }
  }, [user])

  useEffect(() => {
    if (!user) return
    if (initialRoute.current) {
      initialRoute.current = false
      return
    }
    reportRealActivity()
  }, [location.pathname]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!user) return
    const timer = window.setInterval(() => {
      refreshUser().catch(() => {})
    }, 60_000)
    return () => window.clearInterval(timer)
  }, [refreshUser, user])

  useEffect(() => {
    if (!deadlineInfo || !user) {
      setRemaining(null)
      return
    }
    const update = () => {
      const seconds = Math.max(
        0,
        Math.ceil((deadlineInfo.deadline - (Date.now() + serverOffset)) / 1000),
      )
      if (seconds <= user.session_policy.warning_seconds) {
        setRemaining(seconds)
      } else {
        setRemaining(null)
      }
      if (seconds === 0) {
        sessionStorage.setItem('auth_exit_reason', JSON.stringify({
          code: deadlineInfo.code,
          message: deadlineInfo.message,
        }))
        logout().finally(() => navigate('/login', { replace: true }))
      }
    }
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [deadlineInfo, navigate, logout, serverOffset, user])

  const continueUsing = async () => {
    lastActivityReportAt.current = Date.now()
    await recordActivity()
    setRemaining(null)
  }

  const exit = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <Modal
      open={remaining !== null}
      title="即将自动下线"
      closable={false}
      maskClosable={false}
      okText="继续在线"
      cancelText="立即下线"
      onOk={continueUsing}
      onCancel={exit}
    >
      <p>
        已接近无操作时限，{remaining ?? 0} 秒后将自动下线。
        点击“继续在线”可重新计时。
      </p>
    </Modal>
  )
}
