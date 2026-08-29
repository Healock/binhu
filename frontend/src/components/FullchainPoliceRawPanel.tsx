import { useEffect, useState } from 'react'
import { Alert, Button, Tag } from 'antd'
import {
  formatUTCTime,
  listFullchainPoliceRawUploads,
  fullchainPoliceRawDownloadUrl,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import AppTable from './AppTable'
import { Panel } from './ui'

export default function FullchainPoliceRawPanel({ enabled }: { enabled: boolean }) {
  const { systemTimezone } = useAuth()
  const [history, setHistory] = useState<Awaited<ReturnType<typeof listFullchainPoliceRawUploads>>['data']>([])
  const [loading, setLoading] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)

  const loadHistory = async () => {
    if (!enabled) return
    setLoading(true)
    try {
      setHistory((await listFullchainPoliceRawUploads()).data)
      setLoadFailed(false)
    } catch {
      setLoadFailed(true)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { void loadHistory() }, [enabled])

  return (
    <Panel
      title="历史公安网原始数据（只读）"
      description="公安网原始数据比对已经停用；历史文件继续按原权限保留，供审计和倒查下载。"
    >
      <div className="grid gap-4">
        <Alert
          type="info"
          showIcon
          message="不再用于已登记归档判断"
          description="新的已登记归档只认居住证双周期自动确认、本地任务确认及完整 24 小时保留期；本区文件不会影响候选结果。"
        />
        {loadFailed && (
          <Alert
            type="warning"
            showIcon
            message="历史文件读取失败"
            description={<Button type="link" className="p-0" onClick={() => void loadHistory()}>重新加载</Button>}
          />
        )}
        <AppTable
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={history}
          pagination={false}
          locale={{ emptyText: '没有历史公安网原始数据' }}
          scroll={{ x: 720 }}
          columns={[
            { title: '文件', dataIndex: 'file_name', width: 260, ellipsis: true },
            { title: '有效记录', dataIndex: 'row_count', width: 110 },
            { title: '无效/重复', width: 120, render: (_, item) => `${item.invalid_count}/${item.duplicate_count}` },
            { title: '用途', width: 130, render: () => <Tag>历史只读</Tag> },
            { title: '上传时间', dataIndex: 'created_at', width: 190, render: value => formatUTCTime(value, systemTimezone) },
            { title: '原始文件', width: 100, fixed: 'right', render: (_: unknown, item) => <Button type="link" href={fullchainPoliceRawDownloadUrl(item.id)}>下载</Button> },
          ]}
        />
      </div>
    </Panel>
  )
}
