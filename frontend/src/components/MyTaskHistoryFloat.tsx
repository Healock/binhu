import { useState } from 'react'
import { Alert, Button, Drawer, Empty, FloatButton, List, Spin, Tag } from 'antd'
import { HistoryOutlined, ReloadOutlined } from '@ant-design/icons'
import { getMyTaskHistory, type MyTaskHistoryItem } from '../api/client'
import useMobileViewport from '../hooks/useMobileViewport'
import useSystemTime from '../hooks/useSystemTime'

export default function MyTaskHistoryFloat() {
  const mobile = useMobileViewport()
  const formatDateTime = useSystemTime()
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [items, setItems] = useState<MyTaskHistoryItem[]>([])
  const [error, setError] = useState(false)
  const load = async () => {
    setLoading(true)
    setError(false)
    try {
      const response = await getMyTaskHistory()
      const rows = Array.isArray(response?.data) ? response.data : []
      setItems(rows.filter(Boolean).map((item, index) => ({
        id: Number(item?.id) || index,
        action: typeof item?.action === 'string' ? item.action : '',
        target_type: typeof item?.target_type === 'string' ? item.target_type : '',
        task_key: typeof item?.task_key === 'string' ? item.task_key : '',
        result: typeof item?.result === 'string' ? item.result : '',
        created_at: typeof item?.created_at === 'string' ? item.created_at : '',
      })))
    } catch {
      setError(true)
    } finally { setLoading(false) }
  }
  return <>
    <FloatButton icon={<HistoryOutlined />} tooltip="我的任务记录" aria-label="打开我的任务记录" onClick={() => { setOpen(true); void load() }} />
    <Drawer title="我的任务记录" open={open} onClose={() => setOpen(false)} placement={mobile ? 'bottom' : 'right'} width={mobile ? undefined : 440} height={mobile ? '82vh' : undefined} extra={<Button type="text" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新</Button>}>
      {error && <Alert type="error" showIcon message="任务记录暂时加载失败" description="请稍后重试；当前窗口不会关闭。" action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
      {loading && !items.length ? <Spin /> : !loading && !error && !items.length ? <Empty description="暂无本人操作过的任务" /> : !error && items.length > 0 ? <List dataSource={items} renderItem={item => <List.Item><List.Item.Meta title={<span>{item.task_key || '未命名任务'}</span>} description={<span>{formatDateTime(item.created_at)} · {item.action || '任务操作'} <Tag>{item.result || '成功'}</Tag></span>} /></List.Item>} /> : null}
    </Drawer>
  </>
}

