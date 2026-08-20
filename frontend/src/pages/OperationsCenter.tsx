import {
  Alert,
  Badge,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Progress,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  TimePicker,
  Tooltip,
  message,
} from 'antd'
import {
  CloudDownloadOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  FileSearchOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import dayjs, { Dayjs } from 'dayjs'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  downloadBackup,
  downloadDiagnostics,
  getAuditEvents,
  getBackups,
  getOpsDatabaseTables,
  getOpsDatabases,
  getOpsOverview,
  getOpsTableStructure,
  triggerDatabaseBackup,
  updateBackupSchedule,
} from '../api/client'
import type {
  AuditEvent,
  AuditActionOption,
  BackupJob,
  BackupSchedule,
  OpsContainer,
  OpsDatabase,
  OpsOverview,
} from '../types'
import { ListToolbar, Panel } from '../components/ui'
import useSystemTime from '../hooks/useSystemTime'


const STATUS_COLORS: Record<string, string> = {
  running: 'processing',
  pending: 'warning',
  success: 'success',
  completed: 'success',
  healthy: 'success',
  failed: 'error',
  denied: 'error',
  partial: 'warning',
  duplicate: 'default',
  conflict: 'warning',
  unhealthy: 'error',
  unavailable: 'error',
  expired: 'default',
}

const STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  pending: '等待中',
  success: '成功',
  completed: '成功',
  failed: '失败',
  denied: '已拒绝',
  partial: '部分成功',
  duplicate: '重复文件',
  conflict: '发生冲突',
  expired: '已过期',
  healthy: '健康',
  unhealthy: '异常',
  unavailable: '不可用',
  manual: '手动',
  scheduled: '自动',
}

const TXDOCS_SOURCE_LABELS: Record<string, string> = {
  full_sync: '全量同步',
  online_query: '在线数据操作',
  local_writeback: '平台异步写回',
  photo_sheet: '调照片名单',
  unknown: '未标记来源',
}

const TXDOCS_ENDPOINT_LABELS: Record<string, string> = {
  file_info: '文件信息',
  range_read: '范围读取',
  batch_update: '批量写入',
  other: '其他接口',
}

function formatBytes(value?: number | null) {
  if (value == null) return '-'
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let current = value / 1024
  let index = 0
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024
    index += 1
  }
  return `${current.toFixed(current >= 10 ? 1 : 2)} ${units[index]}`
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function StatusTag({ value, label }: { value?: string | null; label?: string | null }) {
  const normalized = value || 'unknown'
  return (
    <Tag color={STATUS_COLORS[normalized] || 'default'}>
      {label || STATUS_LABELS[normalized] || normalized}
    </Tag>
  )
}

const OAUTH_STATUS = {
  not_configured: { status: 'error' as const, text: '未配置' },
  unknown: { status: 'warning' as const, text: '未设置过期时间' },
  expired: { status: 'error' as const, text: '已过期' },
  expiring: { status: 'warning' as const, text: '7 天内过期' },
  healthy: { status: 'success' as const, text: '正常' },
}

function OverviewTab({
  data,
  loading,
  refresh,
}: {
  data: OpsOverview | null
  loading: boolean
  refresh: () => void
}) {
  const formatTime = useSystemTime()
  if (!data && !loading) return <Empty description="运行状态暂时不可用" />
  const diskUsedPercent = data?.disk.total_bytes
    ? Math.round((data.disk.used_bytes / data.disk.total_bytes) * 100)
    : 0
  const requestUsage = data?.txdocs_request_usage
  const quotaExhausted = Boolean(requestUsage?.today.quota_exhausted_responses)
  const photoOutbox = data?.photo_sheet_outbox
  const requestPercent = requestUsage?.daily_limit
    ? Math.min(100, Math.round((requestUsage.today.attempts / requestUsage.daily_limit) * 100))
    : 0
  return (
    <div className="min-w-0 space-y-5">
      <div className="flex justify-end">
        <Button icon={<ReloadOutlined />} onClick={refresh} loading={loading}>刷新状态</Button>
      </div>

      {data?.container_error && (
        <Alert type="warning" showIcon message="容器状态暂时不可用" description={data.container_error} />
      )}
      {!!photoOutbox?.paused && (
        <Alert
          type="warning"
          showIcon
          message={`${photoOutbox.paused} 条调照片腾讯写回已暂停`}
          description="连续失败达到自动重试上限，系统已停止刷写；请到工单流程配置的“同步记录与异常”中核对并手动恢复。"
        />
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <Card>
          <Statistic title="磁盘可用空间" value={formatBytes(data?.disk.free_bytes)} />
          <Progress
            className="mt-3"
            percent={diskUsedPercent}
            status={diskUsedPercent >= 90 ? 'exception' : diskUsedPercent >= 80 ? 'active' : 'normal'}
            format={value => `已用 ${value}%`}
          />
        </Card>
        <Card>
          <Statistic
            title="MySQL 连接"
            value={data?.mysql.connected ? `${data.mysql.connections || 0} / ${data.mysql.max_connections || 0}` : '不可用'}
            prefix={<DatabaseOutlined />}
          />
          <div className="mt-2 text-xs text-slate-500">
            {data?.mysql.version ? `MySQL ${data.mysql.version}` : data?.mysql.error || '-'}
          </div>
        </Card>
        <Card>
          <Statistic
            title="最近同步"
            value={data?.latest_sync ? `#${data.latest_sync.id}` : '-'}
          />
          <div className="mt-2">
            {data?.latest_sync && <StatusTag value={data.latest_sync.status} />}
            <span className="text-xs text-slate-500">{formatTime(data?.latest_sync?.finished_at)}</span>
          </div>
        </Card>
        <Card>
          <Statistic
            title="最近备份"
            value={data?.latest_backup ? `#${data.latest_backup.id}` : '-'}
          />
          <div className="mt-2">
            {data?.latest_backup && <StatusTag value={data.latest_backup.status} />}
            <span className="text-xs text-slate-500">{formatTime(data?.latest_backup?.finished_at)}</span>
          </div>
        </Card>
        <Card>
          <Statistic
            title="调照片待写回"
            value={(photoOutbox?.pending || 0) + (photoOutbox?.retry || 0)}
          />
          <div className="mt-2 text-xs text-slate-500">
            重试中 {photoOutbox?.retry || 0} · 已暂停 {photoOutbox?.paused || 0}
          </div>
        </Card>
      </div>

      <Panel
        title="腾讯接口请求额度"
        description={`按${requestUsage?.timezone || '系统'}时区统计本服务器实际发出的 HTTP 请求尝试；共用同一腾讯应用的其他程序不在本地计数内`}
      >
        {requestUsage && !requestUsage.today_coverage_complete && (
          <Alert
            className="mb-4"
            type="info"
            showIcon
            message="当前业务日的本地计数尚不完整"
            description={`计数从 ${formatTime(requestUsage.metering_started_at)} 开始，部署前已经发生的请求无法还原；收到 400011 后仍会直接按额度耗尽处理。`}
          />
        )}
        {quotaExhausted && (
          <Alert
            className="mb-4"
            type="error"
            showIcon
            message="腾讯接口已返回 400011，当前业务日按额度耗尽处理"
            description="即使本地计数低于日限额，也可能有其他服务器或程序共用同一应用额度。"
          />
        )}
        <Descriptions
          className="mb-3"
          bordered
          size="small"
          column={{ xs: 1, sm: 2, lg: 4 }}
          items={[
            { key: 'attempts', label: '今日请求尝试', children: requestUsage?.today.attempts ?? 0 },
            { key: 'remaining', label: '本地估算剩余', children: requestUsage?.today.estimated_remaining ?? '-' },
            { key: 'success', label: '成功', children: requestUsage?.today.success ?? 0 },
            { key: 'failure', label: '失败 / 重试', children: `${requestUsage?.today.failure ?? 0} / ${requestUsage?.today.retries ?? 0}` },
          ]}
        />
        <Progress
          className="mb-4"
          percent={requestPercent}
          status={quotaExhausted ? 'exception' : requestPercent >= 85 ? 'active' : 'normal'}
          format={() => `${requestUsage?.today.attempts ?? 0} / ${requestUsage?.daily_limit ?? 0}`}
        />
        <Table
          size="small"
          rowKey="business_date"
          pagination={false}
          scroll={{ x: 760 }}
          locale={{ emptyText: '尚未开始记录接口请求' }}
          dataSource={requestUsage?.daily || []}
          columns={[
            { title: '业务日期', dataIndex: 'business_date' },
            { title: '请求尝试', dataIndex: 'attempts', align: 'right' as const },
            { title: '成功', dataIndex: 'success', align: 'right' as const },
            { title: '失败', dataIndex: 'failure', align: 'right' as const },
            { title: '重试请求', dataIndex: 'retries', align: 'right' as const },
            { title: '400011', dataIndex: 'quota_exhausted_responses', align: 'right' as const },
            { title: '估算剩余', dataIndex: 'estimated_remaining', align: 'right' as const },
          ]}
        />
        <div className="mt-4 text-sm font-medium text-[var(--app-text-strong)]">当前业务日请求构成</div>
        <Table
          className="mt-2"
          size="small"
          rowKey={row => `${row.source}:${row.endpoint}:${row.method}`}
          pagination={false}
          locale={{ emptyText: '暂无请求来源记录' }}
          dataSource={requestUsage?.today_breakdown || []}
          columns={[
            {
              title: '来源',
              dataIndex: 'source',
              render: (value: string) => TXDOCS_SOURCE_LABELS[value] || value,
            },
            {
              title: '接口类型',
              dataIndex: 'endpoint',
              render: (value: string) => TXDOCS_ENDPOINT_LABELS[value] || value,
            },
            { title: '方法', dataIndex: 'method' },
            { title: '请求尝试', dataIndex: 'attempts', align: 'right' as const },
            { title: '成功', dataIndex: 'success', align: 'right' as const },
            { title: '失败', dataIndex: 'failure', align: 'right' as const },
            { title: '重试请求', dataIndex: 'retries', align: 'right' as const },
          ]}
        />
      </Panel>

      <Panel
        title="腾讯同步任务次数"
        description={`按${data?.sync_timezone || '系统'}时区统计最近 14 个业务日；一次任务通常包含多次接口请求`}
      >
        <Table
          size="small"
          rowKey="business_date"
          pagination={false}
          scroll={{ x: 720 }}
          locale={{ emptyText: '暂无同步记录' }}
          dataSource={data?.sync_daily_counts || []}
          columns={[
            {
              title: '业务日期',
              dataIndex: 'business_date',
              render: (value: string) => dayjs(value).format('YYYY-MM-DD'),
            },
            { title: '总次数', dataIndex: 'total', align: 'right' as const },
            { title: '成功', dataIndex: 'success', align: 'right' as const },
            { title: '部分成功', dataIndex: 'partial', align: 'right' as const },
            { title: '失败', dataIndex: 'failed', align: 'right' as const },
            { title: '未完成', dataIndex: 'unfinished', align: 'right' as const },
            { title: '手动', dataIndex: 'manual', align: 'right' as const },
            { title: '自动', dataIndex: 'scheduled', align: 'right' as const },
          ]}
        />
      </Panel>

      <Panel title="容器状态" description="内存按 Docker 口径显示工作内存，可回收文件缓存单独列出；只读显示，不提供重启或命令执行">
        <div className="grid gap-4 lg:grid-cols-2">
          {(data?.containers || []).map((container: OpsContainer) => {
            const memoryPercent = container.memory_limit_bytes
              ? Math.round(((container.memory_used_bytes || 0) / container.memory_limit_bytes) * 100)
              : 0
            return (
              <Card key={container.source} size="small">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div>
                    <div className="font-medium text-slate-900">{container.name}</div>
                    <div className="text-xs text-slate-500">{container.image || '-'}</div>
                  </div>
                  <StatusTag value={container.health || container.status} />
                </div>
                <Descriptions
                  size="small"
                  column={2}
                  items={[
                    { key: 'cpu', label: 'CPU', children: `${container.cpu_percent || 0}%` },
                    { key: 'restart', label: '重启次数', children: container.restart_count ?? '-' },
                    { key: 'memory', label: '工作内存', children: `${formatBytes(container.memory_used_bytes)} / ${formatBytes(container.memory_limit_bytes)}` },
                    { key: 'cache', label: '可回收缓存', children: formatBytes(container.memory_cache_bytes) },
                    { key: 'started', label: '启动时间', children: formatTime(container.started_at) },
                  ]}
                />
                <Progress percent={memoryPercent} size="small" format={value => `工作内存 ${value}%`} />
              </Card>
            )
          })}
        </div>
      </Panel>

      <Panel title="凭据健康" description="只显示是否配置和过期时间，不显示令牌内容">
        <Descriptions
          column={{ xs: 1, sm: 2 }}
          items={[
            {
              key: 'configured',
              label: '腾讯文档 OAuth',
              children: data?.oauth
                ? (
                  <Badge
                    status={OAUTH_STATUS[data.oauth.status].status}
                    text={OAUTH_STATUS[data.oauth.status].text}
                  />
                )
                : '-',
            },
            { key: 'expires', label: '过期时间', children: formatTime(data?.oauth.expires_at) },
          ]}
        />
      </Panel>
    </div>
  )
}

interface LogLine {
  stream: string
  message: string
}

function LogsTab() {
  const [source, setSource] = useState('backend')
  const [tail, setTail] = useState(300)
  const [sinceMinutes, setSinceMinutes] = useState(15)
  const [keyword, setKeyword] = useState('')
  const [paused, setPaused] = useState(false)
  const [connected, setConnected] = useState(false)
  const [lines, setLines] = useState<LogLine[]>([])
  const terminalRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setLines([])
    setConnected(false)
    const stream = new EventSource(
      `/api/admin/ops/logs/stream?source=${encodeURIComponent(source)}&tail=${tail}&since_minutes=${sinceMinutes}`,
      { withCredentials: true },
    )
    stream.onopen = () => setConnected(true)
    stream.onerror = () => setConnected(false)
    stream.onmessage = event => {
      try {
        const line = JSON.parse(event.data) as LogLine
        setLines(current => [...current, line].slice(-2000))
      } catch {
        // Ignore malformed transport events; the connection will continue.
      }
    }
    stream.addEventListener('warning', event => {
      try {
        const payload = JSON.parse((event as MessageEvent).data)
        setLines(current => [
          ...current,
          { stream: 'stderr', message: `[连接提示] ${payload.message}` },
        ].slice(-2000))
      } catch {
        // Keep the previous lines.
      }
    })
    return () => stream.close()
  }, [source, tail, sinceMinutes])

  useEffect(() => {
    if (!paused && terminalRef.current) {
      terminalRef.current.scrollTop = terminalRef.current.scrollHeight
    }
  }, [lines, paused])

  const visibleLines = useMemo(() => {
    const query = keyword.trim().toLocaleLowerCase()
    return query
      ? lines.filter(line => line.message.toLocaleLowerCase().includes(query))
      : lines
  }, [keyword, lines])

  const exportUrl = `/api/admin/ops/logs/export?source=${encodeURIComponent(source)}&since_minutes=${sinceMinutes}`

  return (
    <div className="space-y-3">
      <Alert
        type="info"
        showIcon
        message="只读日志（非服务器终端）；密码、令牌、Cookie 和 Authorization 会在服务端自动遮盖。"
      />
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={source}
          onChange={setSource}
          className="w-44"
          options={[
            { value: 'backend', label: '后端日志' },
            { value: 'mysql', label: 'MySQL 错误日志' },
          ]}
        />
        <Select
          value={sinceMinutes}
          onChange={setSinceMinutes}
          className="w-36"
          options={[
            { value: 15, label: '最近 15 分钟' },
            { value: 60, label: '最近 1 小时' },
            { value: 360, label: '最近 6 小时' },
            { value: 1440, label: '最近 24 小时' },
          ]}
        />
        <Select
          value={tail}
          onChange={setTail}
          className="w-32"
          options={[100, 300, 1000, 2000].map(value => ({ value, label: `${value} 行` }))}
        />
        <Input.Search
          value={keyword}
          onChange={event => setKeyword(event.target.value)}
          allowClear
          placeholder="搜索当前日志"
          className="min-w-52 max-w-80"
        />
        <Button
          icon={paused ? <PlayCircleOutlined /> : <PauseOutlined />}
          onClick={() => setPaused(value => !value)}
        >
          {paused ? '继续滚动' : '暂停滚动'}
        </Button>
        <Button onClick={() => setLines([])}>清空屏幕</Button>
        <Button
          icon={<DownloadOutlined />}
          href={exportUrl}
        >
          下载日志
        </Button>
        <Badge status={connected ? 'success' : 'warning'} text={connected ? '实时连接' : '正在重连'} />
      </div>

      <div
        ref={terminalRef}
        className="h-[480px] overflow-auto rounded-xl border border-slate-800 bg-slate-950 p-4 font-mono text-xs leading-5 text-slate-200 shadow-inner"
        role="log"
        aria-live="polite"
      >
        {visibleLines.length ? visibleLines.map((line, index) => (
          <div
            key={`${index}-${line.message.slice(0, 40)}`}
            className={line.stream === 'stderr' ? 'text-amber-300' : 'text-emerald-300'}
          >
            {line.message}
          </div>
        )) : (
          <div className="text-slate-500">等待日志内容……</div>
        )}
      </div>
      <div className="text-xs text-slate-500">
        页面最多保留最近 2,000 行；下载文件最多 10MB。
      </div>
    </div>
  )
}

function DatabasesTab() {
  const formatTime = useSystemTime()
  const [databases, setDatabases] = useState<OpsDatabase[]>([])
  const [selected, setSelected] = useState('OnlineData')
  const [tables, setTables] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [drawer, setDrawer] = useState<any | null>(null)
  const [drawerLoading, setDrawerLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    Promise.all([getOpsDatabases(), getOpsDatabaseTables(selected)])
      .then(([databaseData, tableData]) => {
        setDatabases(databaseData)
        setTables(tableData)
      })
      .catch(() => message.error('数据库概况加载失败'))
      .finally(() => setLoading(false))
  }, [selected])

  const openStructure = async (table: string) => {
    setDrawerLoading(true)
    setDrawer({ database: selected, table, columns: [], indexes: [] })
    try {
      setDrawer(await getOpsTableStructure(selected, table))
    } catch {
      message.error('表结构加载失败')
      setDrawer(null)
    } finally {
      setDrawerLoading(false)
    }
  }

  return (
    <div className="space-y-5">
      <div className="grid gap-4 md:grid-cols-3">
        {databases.map(database => (
          <Card
            key={database.name}
            hoverable
            className={selected === database.name ? 'border-blue-400' : ''}
            onClick={() => setSelected(database.name)}
          >
            <div className="mb-2 font-medium text-slate-900">{database.name}</div>
            <div className="mb-4 text-xs text-slate-500">{database.purpose}</div>
            <Descriptions
              size="small"
              column={1}
              items={[
                { key: 'tables', label: '数据表', children: database.table_count },
                { key: 'rows', label: '估算行数', children: database.estimated_rows.toLocaleString() },
                { key: 'size', label: '占用空间', children: formatBytes(database.data_bytes + database.index_bytes) },
                { key: 'active', label: '最近业务活动', children: formatTime(database.last_activity_at) },
              ]}
            />
          </Card>
        ))}
      </div>

      <Panel title={`${selected} 数据表`} description="行数为 MySQL 估算值；这里只能查看结构，不能修改数据">
        <Table
          rowKey="name"
          loading={loading}
          dataSource={tables}
          scroll={{ x: 900 }}
          pagination={{ defaultPageSize: 20, showSizeChanger: true }}
          columns={[
            { title: '表名', dataIndex: 'name', width: 280, ellipsis: true },
            { title: '引擎', dataIndex: 'engine', width: 100 },
            {
              title: '估算行数',
              dataIndex: 'estimated_rows',
              width: 120,
              sorter: (a, b) => a.estimated_rows - b.estimated_rows,
              render: value => Number(value).toLocaleString(),
            },
            {
              title: '占用空间',
              key: 'size',
              width: 130,
              sorter: (a, b) => (a.data_bytes + a.index_bytes) - (b.data_bytes + b.index_bytes),
              render: (_, row) => formatBytes(row.data_bytes + row.index_bytes),
            },
            { title: '排序规则', dataIndex: 'collation', width: 200 },
            {
              title: '操作',
              key: 'action',
              fixed: 'right',
              width: 100,
              render: (_, row) => (
                <Button type="link" icon={<FileSearchOutlined />} onClick={() => openStructure(row.name)}>
                  结构
                </Button>
              ),
            },
          ]}
        />
      </Panel>

      <Drawer
        open={Boolean(drawer)}
        onClose={() => setDrawer(null)}
        width="min(720px, 100vw)"
        title={drawer ? `${drawer.database}.${drawer.table}` : '表结构'}
      >
        <Table
          rowKey="name"
          loading={drawerLoading}
          dataSource={drawer?.columns || []}
          pagination={false}
          size="small"
          scroll={{ x: 600 }}
          columns={[
            { title: '字段', dataIndex: 'name', width: 180 },
            { title: '类型', dataIndex: 'type', width: 180 },
            { title: '允许空', dataIndex: 'nullable', width: 90, render: value => value ? '是' : '否' },
            { title: '键', dataIndex: 'key', width: 80 },
            { title: '其他', dataIndex: 'extra', width: 160 },
          ]}
        />
        <div className="mb-2 mt-6 font-medium">索引</div>
        <Table
          rowKey={(row, index) => `${row.name}-${row.position}-${index}`}
          dataSource={drawer?.indexes || []}
          pagination={false}
          size="small"
          columns={[
            { title: '索引', dataIndex: 'name' },
            { title: '字段', dataIndex: 'column' },
            { title: '唯一', dataIndex: 'unique', render: value => value ? '是' : '否' },
            { title: '类型', dataIndex: 'type' },
          ]}
        />
      </Drawer>
    </div>
  )
}

function BackupsTab() {
  const formatTime = useSystemTime()
  const [jobs, setJobs] = useState<BackupJob[]>([])
  const [legacy, setLegacy] = useState<Array<{ filename: string; size_bytes: number; modified_at: string }>>([])
  const [schedule, setSchedule] = useState<BackupSchedule | null>(null)
  const [enabled, setEnabled] = useState(true)
  const [runTime, setRunTime] = useState<Dayjs>(dayjs().hour(2).minute(0))
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [downloadJob, setDownloadJob] = useState<BackupJob | null>(null)
  const [password, setPassword] = useState('')
  const [downloading, setDownloading] = useState(false)

  const load = async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const result = await getBackups()
      setJobs(result.data)
      setLegacy(result.legacy_files)
      setSchedule(result.schedule)
      setEnabled(result.schedule.enabled)
      setRunTime(dayjs().hour(result.schedule.run_hour).minute(result.schedule.run_minute))
    } catch {
      if (!silent) message.error('备份记录加载失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(() => load(true), 5000)
    return () => window.clearInterval(timer)
  }, [])

  const create = async () => {
    setCreating(true)
    try {
      const result = await triggerDatabaseBackup()
      message.success(result.message)
      await load(true)
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '备份创建失败')
    } finally {
      setCreating(false)
    }
  }

  const saveSchedule = async () => {
    setSaving(true)
    try {
      const result = await updateBackupSchedule({
        enabled,
        run_hour: runTime.hour(),
        run_minute: runTime.minute(),
      })
      setSchedule(result)
      message.success('每日备份设置已保存')
    } catch {
      message.error('备份设置保存失败')
    } finally {
      setSaving(false)
    }
  }

  const confirmDownload = async () => {
    if (!downloadJob || !password) return
    setDownloading(true)
    try {
      const blob = await downloadBackup(downloadJob.id, password)
      saveBlob(blob, downloadJob.filename || `binhu-backup-${downloadJob.id}.sql.gz`)
      message.success('备份下载已开始')
      setDownloadJob(null)
      setPassword('')
    } catch (error: any) {
      if (error?.response?.data instanceof Blob) {
        try {
          const payload = JSON.parse(await error.response.data.text())
          message.error(payload.detail || '下载失败')
        } catch {
          message.error('下载失败')
        }
      } else {
        message.error(error?.response?.data?.detail || '下载失败')
      }
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="operations-backups-content">
      <Alert
        type="warning"
        showIcon
        message="这里只负责创建、校验和下载备份，不提供恢复或删除"
        description="平台创建的备份保存 7 天，并始终保留最近一份成功备份。"
      />

      <Panel title="每日自动备份" description="默认每天凌晨 2 点备份三个数据库">
        <Space wrap size="middle">
          <Switch checked={enabled} onChange={setEnabled} />
          <span>{enabled ? '已开启' : '已关闭'}</span>
          <TimePicker value={runTime} onChange={value => value && setRunTime(value)} format="HH:mm" minuteStep={5} />
          <Tag color="blue">保存 7 天</Tag>
          <Button type="primary" onClick={saveSchedule} loading={saving}>保存设置</Button>
        </Space>
        <Descriptions
          className="mt-4"
          size="small"
          column={{ xs: 1, sm: 2 }}
          items={[
            { key: 'next', label: '下次自动备份', children: schedule?.enabled ? formatTime(schedule.next_run_at) : '已关闭' },
            { key: 'last', label: '上次自动触发', children: formatTime(schedule?.last_triggered_at) },
          ]}
        />
      </Panel>

      <Panel
        title="备份记录"
        description="每份成功备份都经过 gzip 完整性和 SHA-256 校验"
        extra={(
          <Button type="primary" icon={<CloudDownloadOutlined />} onClick={create} loading={creating}>
            立即备份
          </Button>
        )}
      >
        <Table
          rowKey="id"
          loading={loading}
          dataSource={jobs}
          scroll={{ x: 1100 }}
          pagination={{ defaultPageSize: 20, showSizeChanger: true }}
          columns={[
            { title: '任务', dataIndex: 'id', width: 80, render: value => `#${value}` },
            { title: '来源', dataIndex: 'trigger_source', width: 90, render: value => STATUS_LABELS[value] || value },
            { title: '状态', dataIndex: 'status', width: 100, render: value => <StatusTag value={value} /> },
            { title: '创建时间', dataIndex: 'created_at', width: 180, render: formatTime },
            { title: '完成时间', dataIndex: 'finished_at', width: 180, render: formatTime },
            { title: '大小', dataIndex: 'size_bytes', width: 110, render: formatBytes },
            {
              title: 'SHA-256',
              dataIndex: 'sha256',
              width: 180,
              ellipsis: true,
              render: value => value
                ? <Tooltip title={value}><span className="font-mono text-xs">{value}</span></Tooltip>
                : '-',
            },
            {
              title: '错误',
              dataIndex: 'error_message',
              width: 220,
              ellipsis: { showTitle: false },
              render: value => value ? <Tooltip title={value}>{value}</Tooltip> : '-',
            },
            {
              title: '操作',
              key: 'action',
              fixed: 'right',
              width: 100,
              render: (_, row: BackupJob) => (
                <Button
                  type="link"
                  icon={<DownloadOutlined />}
                  disabled={row.status !== 'success' || !row.filename}
                  onClick={() => setDownloadJob(row)}
                >
                  下载
                </Button>
              ),
            },
          ]}
        />
      </Panel>

      {legacy.length > 0 && (
        <Panel title="历史备份" description="上线本功能以前创建的文件，只展示，不自动清理或下载">
          <Table
            rowKey="filename"
            dataSource={legacy}
            pagination={false}
            columns={[
              { title: '文件名', dataIndex: 'filename', ellipsis: true },
              { title: '大小', dataIndex: 'size_bytes', width: 120, render: formatBytes },
              { title: '修改时间', dataIndex: 'modified_at', width: 190, render: formatTime },
            ]}
          />
        </Panel>
      )}

      <Modal
        open={Boolean(downloadJob)}
        title="重新验证超级管理员密码"
        okText="验证并下载"
        cancelText="取消"
        confirmLoading={downloading}
        onOk={confirmDownload}
        onCancel={() => {
          setDownloadJob(null)
          setPassword('')
        }}
      >
        <p className="mb-3 text-sm text-slate-600">
          整库备份含业务数据，下载前需要重新输入当前账号密码。
        </p>
        <Input.Password
          value={password}
          onChange={event => setPassword(event.target.value)}
          onPressEnter={confirmDownload}
          placeholder="当前账号密码"
          autoComplete="current-password"
        />
      </Modal>
    </div>
  )
}

function AuditTab() {
  const formatTime = useSystemTime()
  const [data, setData] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [total, setTotal] = useState(0)
  const [action, setAction] = useState('')
  const [actionOptions, setActionOptions] = useState<AuditActionOption[]>([])
  const [refreshToken, setRefreshToken] = useState(0)

  useEffect(() => {
    setLoading(true)
    getAuditEvents({ page, page_size: pageSize, action: action || undefined })
      .then(result => {
        setData(result.data)
        setTotal(result.total)
        setActionOptions(result.action_options)
      })
      .catch(() => message.error('操作记录加载失败'))
      .finally(() => setLoading(false))
  }, [action, page, pageSize, refreshToken])

  return (
    <div className="flex flex-col gap-4">
      <ListToolbar
        notice={<Alert type="info" showIcon message="默认显示姓名和中文摘要；展开记录可查看原始审计字段。操作记录不保存密码、令牌、Cookie 或请求正文。" />}
        filters={<Select
          allowClear
          showSearch
          value={action || undefined}
          placeholder="按操作类型筛选"
          className="w-full md:w-96"
          options={actionOptions}
          filterOption={(input, option) => (
            `${option?.label || ''} ${option?.value || ''}`
              .toLocaleLowerCase()
              .includes(input.toLocaleLowerCase())
          )}
          onChange={value => {
            setPage(1)
            setAction(value || '')
          }}
        />}
        meta={<span>共 {total} 条操作记录</span>}
        actions={<Button icon={<ReloadOutlined />} onClick={() => setRefreshToken(value => value + 1)}>刷新</Button>}
      />
      <Table
        rowKey="id"
        loading={loading}
        dataSource={data}
        scroll={{ x: 1100 }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onChange: (nextPage, nextSize) => {
            setPage(nextPage)
            setPageSize(nextSize)
          },
        }}
        columns={[
          { title: '时间', dataIndex: 'created_at', width: 180, render: formatTime },
          {
            title: '操作者',
            dataIndex: 'actor_name',
            width: 140,
            render: value => <span className="font-medium">{value || '系统自动任务'}</span>,
          },
          {
            title: '操作',
            dataIndex: 'action_label',
            width: 190,
            render: value => <span className="font-medium">{value}</span>,
          },
          { title: '目标', dataIndex: 'target_display', width: 240 },
          {
            title: '结果',
            dataIndex: 'result',
            width: 110,
            render: (value, row) => <StatusTag value={value} label={row.result_label} />,
          },
          { title: 'IP', dataIndex: 'ip_address', width: 140 },
          {
            title: '详情',
            dataIndex: 'detail_items',
            width: 360,
            render: items => items?.length
              ? (
                  <Space size={[4, 4]} wrap>
                    {items.slice(0, 4).map(item => (
                      <Tag key={item.key} className="max-w-64 truncate" title={`${item.label}：${item.value}`}>
                        {item.label}：{item.value}
                      </Tag>
                    ))}
                    {items.length > 4 && <Tag>另有 {items.length - 4} 项</Tag>}
                  </Space>
                )
              : '-',
          },
        ]}
        expandable={{
          expandIcon: ({ expanded, onExpand, record }) => (
            <button
              type="button"
              className="compact-action inline-flex h-11 w-11 items-center justify-center rounded-md text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800 md:h-7 md:w-7 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100"
              aria-label={expanded ? '收起操作详情' : '展开操作详情'}
              onClick={event => {
                event.stopPropagation()
                onExpand(record, event)
              }}
            >
              <RightOutlined
                className={`text-xs transition-transform${expanded ? ' rotate-90' : ''}`}
              />
            </button>
          ),
          expandedRowRender: row => (
            <div className="space-y-3 px-2 py-1">
              <Descriptions
                size="small"
                column={{ xs: 1, sm: 2, lg: 3 }}
                items={[
                  { key: 'account', label: '当前账号', children: row.actor_account || '-' },
                  { key: 'recorded-account', label: '记录时账号', children: row.username || '-' },
                  { key: 'action', label: '操作代码', children: row.action || '-' },
                  {
                    key: 'target',
                    label: '原始目标',
                    children: `${row.target_type || '-'} · ${row.target_name || '-'}`,
                  },
                  { key: 'ip', label: '来源 IP', children: row.ip_address || '-' },
                  { key: 'agent', label: '浏览器', children: row.user_agent || '-' },
                ]}
              />
              <details className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700">
                <summary className="cursor-pointer text-sm text-slate-600 dark:text-slate-300">
                  查看原始审计详情
                </summary>
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-950 p-3 text-xs text-slate-100">
                  {row.detail ? JSON.stringify(row.detail, null, 2) : '无原始详情'}
                </pre>
              </details>
            </div>
          ),
        }}
      />
    </div>
  )
}

export default function OperationsCenter() {
  const [overview, setOverview] = useState<OpsOverview | null>(null)
  const [overviewLoading, setOverviewLoading] = useState(true)
  const [diagnosing, setDiagnosing] = useState(false)

  const loadOverview = async () => {
    setOverviewLoading(true)
    try {
      setOverview(await getOpsOverview())
    } catch {
      message.error('运行概况加载失败')
    } finally {
      setOverviewLoading(false)
    }
  }

  useEffect(() => {
    loadOverview()
  }, [])

  const diagnostics = async () => {
    setDiagnosing(true)
    try {
      saveBlob(await downloadDiagnostics(), 'binhu-diagnostics.zip')
      message.success('诊断包已生成')
    } catch {
      message.error('诊断包生成失败')
    } finally {
      setDiagnosing(false)
    }
  }

  return (
    <div className="space-y-5">
      <Panel
        title="运维中心"
        description="只读查看底层状态，并安全地创建数据库备份"
        extra={(
          <Button icon={<SafetyCertificateOutlined />} onClick={diagnostics} loading={diagnosing}>
            导出诊断包
          </Button>
        )}
      >
        <Alert
          type="success"
          showIcon
          message="安全运维模式"
          description="不提供网页终端、任意 SQL、数据库恢复、备份删除或容器重启。"
        />
      </Panel>

      <Tabs
        className="ops-tabs min-w-0"
        destroyInactiveTabPane={false}
        items={[
          {
            key: 'overview',
            label: '运行概况',
            children: <OverviewTab data={overview} loading={overviewLoading} refresh={loadOverview} />,
          },
          { key: 'logs', label: '系统日志', children: <LogsTab /> },
          { key: 'databases', label: '数据库', children: <DatabasesTab /> },
          { key: 'backups', label: '备份管理', children: <BackupsTab /> },
          { key: 'audit', label: '操作记录', children: <AuditTab /> },
        ]}
      />
    </div>
  )
}
