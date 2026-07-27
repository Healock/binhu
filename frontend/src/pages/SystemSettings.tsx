import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  InputNumber,
  Select,
  Space,
  Switch,
} from 'antd'
import {
  formatUTCTime,
  getSyncSchedule,
  getSystemConfig,
  updateSyncSchedule,
  updateSystemConfig,
} from '../api/client'
import type { SyncSchedule } from '../types'
import { Panel } from '../components/ui'
import {
  formatCountdown,
  getRemainingTime,
  getServerOffset,
} from '../utils/countdown'

const TIMEZONES = [
  { value: 'Asia/Shanghai', label: '上海 (UTC+8)' },
  { value: 'Asia/Urumqi', label: '乌鲁木齐 (UTC+6)' },
  { value: 'Asia/Tokyo', label: '东京 (UTC+9)' },
  { value: 'Asia/Singapore', label: '新加坡 (UTC+8)' },
  { value: 'UTC', label: 'UTC (UTC+0)' },
  { value: 'America/New_York', label: '纽约 (UTC-5)' },
  { value: 'Europe/London', label: '伦敦 (UTC+0)' },
]

const INTERVAL_OPTIONS = [
  { value: 5, label: '每 5 分钟' },
  { value: 15, label: '每 15 分钟' },
  { value: 30, label: '每 30 分钟' },
  { value: 60, label: '每 1 小时' },
  { value: 120, label: '每 2 小时' },
  { value: 240, label: '每 4 小时' },
  { value: 480, label: '每 8 小时' },
  { value: 720, label: '每 12 小时' },
  { value: 1440, label: '每 24 小时' },
  { value: 'custom', label: '自定义间隔' },
]
const COMMON_INTERVALS = new Set(
  INTERVAL_OPTIONS
    .map(option => option.value)
    .filter((value): value is number => typeof value === 'number'),
)

export default function SystemSettings() {
  const [timezone, setTimezone] = useState('Asia/Shanghai')
  const [schedule, setSchedule] = useState<SyncSchedule>({
    enabled: true,
    interval_minutes: 5,
    next_run_at: null,
    server_time: null,
  })
  const [enabled, setEnabled] = useState(true)
  const [interval, setIntervalValue] = useState(5)
  const [intervalChoice, setIntervalChoice] = useState<number | 'custom'>(5)
  const [loading, setLoading] = useState(true)
  const [savingTimezone, setSavingTimezone] = useState(false)
  const [savingSchedule, setSavingSchedule] = useState(false)
  const [timezoneMsg, setTimezoneMsg] = useState('')
  const [scheduleMsg, setScheduleMsg] = useState('')
  const [clock, setClock] = useState(Date.now())

  useEffect(() => {
    Promise.all([getSystemConfig(), getSyncSchedule()])
      .then(([config, currentSchedule]) => {
        setTimezone(config.timezone || 'Asia/Shanghai')
        setSchedule(currentSchedule)
        setEnabled(currentSchedule.enabled)
        setIntervalValue(currentSchedule.interval_minutes)
        setIntervalChoice(
          COMMON_INTERVALS.has(currentSchedule.interval_minutes)
            ? currentSchedule.interval_minutes
            : 'custom',
        )
      })
      .catch(() => setScheduleMsg('系统设置加载失败，请稍后重试'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const serverOffset = useMemo(
    () => getServerOffset(schedule.server_time),
    [schedule.server_time],
  )
  const remaining = getRemainingTime(
    schedule.next_run_at,
    serverOffset,
    clock,
  )
  const countdown = remaining == null
    ? '-'
    : formatCountdown(remaining)

  const handleSaveTimezone = async () => {
    setSavingTimezone(true)
    setTimezoneMsg('')
    try {
      await updateSystemConfig({ timezone })
      setTimezoneMsg('保存成功，刷新页面后生效')
    } catch {
      setTimezoneMsg('保存失败')
    } finally {
      setSavingTimezone(false)
    }
  }

  const handleIntervalChoice = (value: number | 'custom') => {
    setIntervalChoice(value)
    if (typeof value === 'number') setIntervalValue(value)
  }

  const handleSaveSchedule = async () => {
    setSavingSchedule(true)
    setScheduleMsg('')
    try {
      const result = await updateSyncSchedule({
        enabled,
        interval_minutes: interval,
      })
      setSchedule(result)
      setScheduleMsg(
        enabled ? '定时同步已保存，倒计时已重新开始' : '定时同步已关闭',
      )
    } catch (error: any) {
      setScheduleMsg(error?.response?.data?.detail || '保存失败')
    } finally {
      setSavingSchedule(false)
    }
  }

  return (
    <div className="space-y-6">
      <Panel
        title="自动同步"
        description="按固定间隔读取腾讯文档；修改设置后会重新开始倒计时"
      >
        <div className="space-y-5">
          <div>
            <div className="mb-2 text-sm font-medium text-slate-700">启用状态</div>
            <Space>
              <Switch
                checked={enabled}
                onChange={setEnabled}
                loading={loading}
              />
              <span className="text-sm text-slate-600">
                {enabled ? '已开启自动同步' : '已关闭自动同步'}
              </span>
            </Space>
          </div>

          <div>
            <label className="mb-2 block text-sm font-medium text-slate-700">
              同步间隔
            </label>
            <Space wrap>
              <Select
                value={intervalChoice}
                onChange={handleIntervalChoice}
                options={INTERVAL_OPTIONS}
                className="w-48"
                disabled={loading}
              />
              {intervalChoice === 'custom' && (
                <InputNumber
                  min={5}
                  max={10080}
                  value={interval}
                  onChange={value => setIntervalValue(value || 5)}
                  addonAfter="分钟"
                  className="w-48"
                />
              )}
            </Space>
            <p className="mt-2 text-xs text-slate-500">
              自定义范围为 5 分钟至 7 天。
            </p>
          </div>

          <Descriptions
            size="small"
            column={{ xs: 1, sm: 2 }}
            items={[
              {
                key: 'countdown',
                label: '距离下次同步',
                children: schedule.enabled ? countdown : '已关闭',
              },
              {
                key: 'next',
                label: '下次执行时间',
                children: schedule.enabled && schedule.next_run_at
                  ? formatUTCTime(schedule.next_run_at, timezone)
                  : '-',
              },
            ]}
          />

          <Button
            type="primary"
            onClick={handleSaveSchedule}
            loading={savingSchedule}
            disabled={loading || interval < 5 || interval > 10080}
          >
            保存自动同步设置
          </Button>
          {scheduleMsg && (
            <Alert
              type={scheduleMsg.includes('失败') ? 'error' : 'success'}
              showIcon
              message={scheduleMsg}
            />
          )}
        </div>
      </Panel>

      <Panel title="系统时间" description="设置系统时间在页面上的显示方式">
        <div className="space-y-4">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">
              系统时区
            </label>
            <p className="mb-2 text-xs text-slate-500">
              数据库保存 UTC 标准时间，页面按照这里选择的时区显示。
            </p>
            <Select
              value={timezone}
              onChange={setTimezone}
              className="w-full md:w-72"
              options={TIMEZONES}
            />
          </div>
          <Button
            type="primary"
            onClick={handleSaveTimezone}
            loading={savingTimezone}
          >
            保存时区
          </Button>
          {timezoneMsg && (
            <Alert
              type={timezoneMsg.includes('成功') ? 'success' : 'error'}
              showIcon
              message={timezoneMsg}
            />
          )}
        </div>
      </Panel>
    </div>
  )
}
