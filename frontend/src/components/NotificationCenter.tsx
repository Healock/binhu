import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Radio,
  Space,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  BellOutlined,
  CheckOutlined,
  DeleteOutlined,
  NotificationOutlined,
} from '@ant-design/icons'
import type { AppNotification } from '../types'
import {
  createAnnouncement,
  deleteAnnouncement,
  formatUTCTime,
  getNotificationUnreadCount,
  getNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { useNavigate } from 'react-router-dom'

interface AnnouncementFormValues {
  title: string
  content: string
  severity: 'info' | 'warning'
}

interface NotificationCenterProps {
  placement?: 'sidebar' | 'mobile-header'
}

export default function NotificationCenter({ placement = 'sidebar' }: NotificationCenterProps) {
  const navigate = useNavigate()
  const { user, systemTimezone } = useAuth()
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [notifications, setNotifications] = useState<AppNotification[]>([])
  const [unreadCount, setUnreadCount] = useState(0)
  const [personalUnread, setPersonalUnread] = useState(0)
  const [announcementUnread, setAnnouncementUnread] = useState(0)
  const [loadError, setLoadError] = useState('')
  const [publishOpen, setPublishOpen] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [form] = Form.useForm<AnnouncementFormValues>()
  const [modal, contextHolder] = Modal.useModal()
  const canPublishAnnouncements = Boolean(user?.permissions.includes('announcement.manage'))

  const load = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true)
    try {
      const result = await getNotifications(50)
      setLoadError('')
      setNotifications(result.data)
      setUnreadCount(result.unread_count)
      setPersonalUnread(result.personal_unread_count)
      setAnnouncementUnread(result.announcement_unread_count)
    } catch {
      setLoadError('消息暂时加载失败，请点击重试；当前页面不会受到影响。')
    } finally {
      if (showLoading) setLoading(false)
    }
  }, [])

  const loadUnreadCount = useCallback(async () => {
    try {
      const result = await getNotificationUnreadCount()
      setUnreadCount(result.unread_count)
      setPersonalUnread(result.personal_unread_count)
      setAnnouncementUnread(result.announcement_unread_count)
    } catch {
      // 未读数轮询失败时保留当前显示，下一轮继续重试。
    }
  }, [])

  useEffect(() => {
    void loadUnreadCount()
    const timer = window.setInterval(() => {
      if (open) {
        void load()
      } else {
        void loadUnreadCount()
      }
    }, 30000)
    return () => window.clearInterval(timer)
  }, [load, loadUnreadCount, open])

  useEffect(() => {
    const refreshCounts = () => void loadUnreadCount()
    window.addEventListener('binhu:notifications-changed', refreshCounts)
    return () => window.removeEventListener('binhu:notifications-changed', refreshCounts)
  }, [loadUnreadCount])

  const announcements = useMemo(
    () => notifications.filter(item => item.source === 'announcement'),
    [notifications],
  )
  const personalNotifications = useMemo(
    () => notifications.filter(item => item.source === 'personal'),
    [notifications],
  )

  const handleOpen = () => {
    setOpen(true)
    void load(true)
  }

  useEffect(() => {
    const openFromDashboard = () => handleOpen()
    window.addEventListener('binhu:open-notification-center', openFromDashboard)
    return () => window.removeEventListener('binhu:open-notification-center', openFromDashboard)
  }, [load])

  const handleRead = async (notification: AppNotification) => {
    if (!notification.is_read) {
      try {
        await markNotificationRead(notification)
        setNotifications(current => current.map(item => (
          item.id === notification.id && item.source === notification.source
            ? { ...item, is_read: true }
            : item
        )))
        setUnreadCount(current => Math.max(0, current - 1))
        if (notification.source === 'announcement') {
          setAnnouncementUnread(current => Math.max(0, current - 1))
        } else {
          setPersonalUnread(current => Math.max(0, current - 1))
        }
      } catch {
        message.error('消息状态更新失败，请稍后重试')
      }
    }
    if (notification.action_path && /^\/(?!\/)[^\r\n\\]*$/.test(notification.action_path)) {
      setOpen(false)
      navigate(notification.action_path)
    }
  }

  const handleReadAll = async () => {
    try {
      await markAllNotificationsRead()
      setNotifications(current => current.map(item => ({
        ...item,
        is_read: true,
      })))
      setUnreadCount(0)
      setPersonalUnread(0)
      setAnnouncementUnread(0)
    } catch {
      message.error('全部已读操作失败，请稍后重试')
    }
  }

  const publishAnnouncement = async () => {
    try {
      const values = await form.validateFields()
      setPublishing(true)
      await createAnnouncement({
        title: values.title.trim(),
        content: values.content.trim(),
        severity: values.severity,
      })
      message.success('公告已发布，所有登录用户都可以看到')
      setPublishOpen(false)
      form.resetFields()
      await load()
    } catch (error) {
      if (
        error
        && typeof error === 'object'
        && 'errorFields' in error
      ) {
        return
      }
      message.error('公告发布失败，请稍后重试')
    } finally {
      setPublishing(false)
    }
  }

  const confirmDelete = (
    event: React.MouseEvent,
    notification: AppNotification,
  ) => {
    event.stopPropagation()
    modal.confirm({
      title: `删除公告“${notification.title}”？`,
      content: '删除后所有用户都不再看到这条公告，操作会记录到系统操作记录。',
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteAnnouncement(notification.id)
          message.success('公告已删除')
          await load()
        } catch {
          message.error('公告删除失败，请稍后重试')
          throw new Error('delete announcement failed')
        }
      },
    })
  }

  const renderTrigger = () => (
    <Badge count={unreadCount} size="small" overflowCount={99}>
      <Button
        type="text"
        shape="circle"
        aria-label="打开消息中心"
        icon={<BellOutlined />}
        onClick={handleOpen}
      />
    </Badge>
  )

  const renderList = (
    items: AppNotification[],
    emptyText: string,
  ) => (
    <List
      className="notification-list"
      loading={loading}
      dataSource={items}
      locale={{ emptyText: <Empty description={emptyText} /> }}
      renderItem={item => (
        <List.Item
          className={[
            'notification-list__item',
            !item.is_read ? 'notification-list__item--unread' : '',
          ].filter(Boolean).join(' ')}
          onClick={() => void handleRead(item)}
          style={{ cursor: !item.is_read || item.action_path ? 'pointer' : 'default' }}
        >
          <div className="flex w-full min-w-0 items-start gap-3">
            <div className="min-w-0 flex-1">
              <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                <Tag
                  bordered={false}
                  className="m-0"
                  color={item.source === 'announcement' ? 'blue' : 'default'}
                >
                  {item.source === 'announcement' ? '公告' : '个人提示'}
                </Tag>
                {item.severity === 'warning' && (
                  <Tag bordered={false} className="m-0" color="orange">重要</Tag>
                )}
                {item.severity === 'error' && (
                  <Tag bordered={false} className="m-0" color="red">异常</Tag>
                )}
                {!item.is_read && (
                  <span className="notification-list__unread-label">
                    <span aria-hidden="true" className="notification-list__unread-dot" />
                    未读
                  </span>
                )}
              </div>
              <Typography.Text
                className="notification-list__title block text-[15px] leading-6"
                strong={!item.is_read}
              >
                {item.title}
              </Typography.Text>
              <Typography.Paragraph
                className="notification-list__content mb-2 mt-1 text-sm"
                ellipsis={{ rows: 5, expandable: true, symbol: '展开' }}
              >
                {item.content}
              </Typography.Paragraph>
              <span className="notification-list__time text-xs">
                {formatUTCTime(item.created_at, systemTimezone)}
              </span>
            </div>
            {canPublishAnnouncements && item.source === 'announcement' && (
              <Tooltip title="删除公告">
                <Button
                  danger
                  type="text"
                  size="small"
                  aria-label={`删除公告 ${item.title}`}
                  icon={<DeleteOutlined />}
                  onClick={event => confirmDelete(event, item)}
                />
              </Tooltip>
            )}
          </div>
        </List.Item>
      )}
    />
  )

  return (
    <>
      {contextHolder}
      {placement === 'mobile-header' ? (
        <div className="mobile-app-header__notification">
          {renderTrigger()}
        </div>
      ) : (
        <div className="shrink-0">
          {renderTrigger()}
        </div>
      )}

      <Drawer
        title="消息中心"
        open={open}
        onClose={() => setOpen(false)}
        width="min(460px, 100vw)"
        extra={(
          <Space size="small">
            {canPublishAnnouncements && (
              <Button
                type="link"
                size="small"
                icon={<NotificationOutlined />}
                onClick={() => setPublishOpen(true)}
              >
                发布公告
              </Button>
            )}
            {unreadCount > 0 && (
              <Button
                type="link"
                size="small"
                icon={<CheckOutlined />}
                onClick={handleReadAll}
              >
                全部已读
              </Button>
            )}
          </Space>
        )}
      >
        {loadError && <Alert className="mb-3" type="warning" showIcon message={loadError} action={<Button size="small" onClick={() => void load(true)}>重试</Button>} />}
        <Tabs
          items={[
            {
              key: 'announcements',
              label: (
                <span>
                  公告
                  {announcementUnread > 0 && (
                    <Badge
                      className="ml-2"
                      count={announcementUnread}
                      size="small"
                    />
                  )}
                </span>
              ),
              children: renderList(announcements, '暂无公告'),
            },
            {
              key: 'personal',
              label: (
                <span>
                  个人提示
                  {personalUnread > 0 && (
                    <Badge
                      className="ml-2"
                      count={personalUnread}
                      size="small"
                    />
                  )}
                </span>
              ),
              children: renderList(personalNotifications, '暂无个人提示'),
            },
          ]}
        />
      </Drawer>

      <Modal
        title="发布公告"
        open={publishOpen}
        okText="发布"
        cancelText="取消"
        confirmLoading={publishing}
        onOk={() => void publishAnnouncement()}
        onCancel={() => {
          setPublishOpen(false)
          form.resetFields()
        }}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ severity: 'info' }}
        >
          <Form.Item
            name="title"
            label="公告标题"
            rules={[
              { required: true, whitespace: true, message: '请输入公告标题' },
              { max: 100, message: '标题不能超过 100 个字' },
            ]}
          >
            <Input placeholder="例如：数据统计口径说明" />
          </Form.Item>
          <Form.Item
            name="content"
            label="公告内容"
            rules={[
              { required: true, whitespace: true, message: '请输入公告内容' },
              { max: 2000, message: '内容不能超过 2000 个字' },
            ]}
          >
            <Input.TextArea
              rows={5}
              showCount
              maxLength={2000}
              placeholder="所有登录用户都能看到这条公告"
            />
          </Form.Item>
          <Form.Item name="severity" label="公告级别">
            <Radio.Group
              options={[
                { label: '普通公告', value: 'info' },
                { label: '重要提醒', value: 'warning' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
