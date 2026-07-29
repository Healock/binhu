import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  Badge,
  Button,
  Drawer,
  Empty,
  List,
  Tag,
  Typography,
} from 'antd'
import { BellOutlined, CheckOutlined } from '@ant-design/icons'
import type { AppNotification } from '../types'
import {
  formatUTCTime,
  getNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from '../api/client'

export default function NotificationCenter() {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [notifications, setNotifications] = useState<AppNotification[]>([])
  const [unreadCount, setUnreadCount] = useState(0)

  const load = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true)
    try {
      const result = await getNotifications(30)
      setNotifications(result.data)
      setUnreadCount(result.unread_count)
    } catch {
      // 全局通知不打断当前页面，下一轮轮询会重试。
    } finally {
      if (showLoading) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 30000)
    return () => window.clearInterval(timer)
  }, [load])

  const handleOpen = () => {
    setOpen(true)
    void load(true)
  }

  const handleRead = async (notification: AppNotification) => {
    if (notification.is_read) return
    await markNotificationRead(notification.id)
    setNotifications(current => current.map(item => (
      item.id === notification.id ? { ...item, is_read: true } : item
    )))
    setUnreadCount(current => Math.max(0, current - 1))
  }

  const handleReadAll = async () => {
    await markAllNotificationsRead()
    setNotifications(current => current.map(item => ({ ...item, is_read: true })))
    setUnreadCount(0)
  }

  const renderTrigger = () => (
    <Badge count={unreadCount} size="small" overflowCount={99}>
      <Button
        type="text"
        shape="circle"
        aria-label="打开站内通知"
        icon={<BellOutlined />}
        onClick={handleOpen}
      />
    </Badge>
  )

  return (
    <>
      {typeof document !== 'undefined' && createPortal(
        <div className="fixed right-3 top-2.5 z-[60] md:hidden">
          {renderTrigger()}
        </div>,
        document.body,
      )}

      <div className="hidden shrink-0 md:block">
        {renderTrigger()}
      </div>

      <Drawer
        title="站内通知"
        open={open}
        onClose={() => setOpen(false)}
        width="min(420px, 100vw)"
        extra={
          unreadCount > 0 ? (
            <Button
              type="link"
              size="small"
              icon={<CheckOutlined />}
              onClick={handleReadAll}
            >
              全部已读
            </Button>
          ) : null
        }
      >
        <List
          loading={loading}
          dataSource={notifications}
          locale={{ emptyText: <Empty description="暂无通知" /> }}
          renderItem={item => (
            <List.Item
              className={!item.is_read ? 'rounded-lg bg-blue-50/70 px-3' : 'px-3'}
              onClick={() => void handleRead(item)}
              style={{ cursor: item.is_read ? 'default' : 'pointer' }}
            >
              <List.Item.Meta
                title={
                  <div className="flex items-center gap-2">
                    <Typography.Text strong={!item.is_read}>
                      {item.title}
                    </Typography.Text>
                    {!item.is_read && <Tag color="blue">未读</Tag>}
                  </div>
                }
                description={
                  <div>
                    <Typography.Paragraph
                      className="mb-1 text-sm"
                      ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
                    >
                      {item.content}
                    </Typography.Paragraph>
                    <span className="text-xs text-slate-400">
                      {formatUTCTime(item.created_at)}
                    </span>
                  </div>
                }
              />
            </List.Item>
          )}
        />
      </Drawer>
    </>
  )
}
