import { useState } from 'react'
import { Button, Drawer, Empty, FloatButton, List, Spin, Tag } from 'antd'
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
  const load = async () => {
    setLoading(true)
    try { setItems((await getMyTaskHistory()).data || []) } finally { setLoading(false) }
  }
  return <>
    <FloatButton icon={<HistoryOutlined />} tooltip="我的任务记录" aria-label="打开我的任务记录" onClick={() => { setOpen(true); void load() }} />
    <Drawer title="我的任务记录" open={open} onClose={() => setOpen(false)} placement={mobile ? 'bottom' : 'right'} width={mobile ? undefined : 440} height={mobile ? '82vh' : undefined} extra={<Button type="text" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新</Button>}>
      {loading && !items.length ? <Spin /> : !items.length ? <Empty description="暂无本人操作过的任务" /> : <List dataSource={items} renderItem={item => <List.Item><List.Item.Meta title={<span>{item.task_key}</span>} description={<span>{formatDateTime(item.created_at)} · {item.action || '任务操作'} <Tag>{item.result || '成功'}</Tag></span>} /></List.Item>} />}
    </Drawer>
  </>
}

