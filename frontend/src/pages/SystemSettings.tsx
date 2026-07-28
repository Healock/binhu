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
import {
  DEFAULT_SUMMARY_POSITIONS,
  PERSONNEL_POSITIONS,
  parseSummaryPositions,
  type PersonnelPosition,
} from '../constants/personnel'

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
  const [onlinePositions, setOnlinePositions] = useState<PersonnelPosition[]>(
    [...DEFAULT_SUMMARY_POSITIONS],
  )
  const [visitPositions, setVisitPositions] = useState<PersonnelPosition[]>(
    [...DEFAULT_SUMMARY_POSITIONS],
  )
  const [savingPositions, setSavingPositions] = useState(false)
  const [positionsMsg, setPositionsMsg] = useState('')
  const [clock, setClock] = useState(Date.now())

  useEffect(() => {
    Promise.all([getSystemConfig(), getSyncSchedule()])
      .then(([config, currentSchedule]) => {
        setTimezone(config.timezone || 'Asia/Shanghai')
        setOnlinePositions(
          parseSummaryPositions(config.online_summary_positions),
        )
        setVisitPositions(
          parseSummaryPositions(config.visit_summary_positions),
        )
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

  const handleSavePositions = async () => {
    if (!onlinePositions.length || !visitPositions.length) {
      setPositionsMsg('在线汇总和走访汇总都至少选择一个岗位')
      return
    }
    setSavingPositions(true)
    setPositionsMsg('')
    try {
      await updateSystemConfig({
        online_summary_positions: JSON.stringify(onlinePositions),
        visit_summary_positions: JSON.stringify(visitPositions),
      })
      setPositionsMsg('统计岗位已保存，重新查询汇总后生效')
    } catch (error: any) {
      setPositionsMsg(error?.response?.data?.detail || '保存失败')
    } finally {
      setSavingPositions(false)
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

      <Panel
        title="汇总统计岗位"
        description="人员仍会保留在人员管理中，只有这里选中的岗位才进入对应汇总"
      >
        <div className="space-y-5">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">
              在线数据汇总
            </label>
            <Select<PersonnelPosition[]>
              mode="multiple"
              value={onlinePositions}
              onChange={setOnlinePositions}
              options={PERSONNEL_POSITIONS.map(position => ({
                value: position,
                label: position,
              }))}
              placeholder="选择参与在线汇总的岗位"
              className="w-full"
              maxTagCount="responsive"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">
              走访汇总
            </label>
            <Select<PersonnelPosition[]>
              mode="multiple"
              value={visitPositions}
              onChange={setVisitPositions}
              options={PERSONNEL_POSITIONS.map(position => ({
                value: position,
                label: position,
              }))}
              placeholder="选择参与走访汇总的岗位"
              className="w-full"
              maxTagCount="responsive"
            />
          </div>
          <Alert
            type="info"
            showIcon
            message="默认统计组长和组员"
            description="人员管理中没有登记的姓名仍会保留在汇总中，避免未知数据被直接隐藏。"
          />
          <Button
            type="primary"
            loading={savingPositions}
            disabled={
              loading
              || !onlinePositions.length
              || !visitPositions.length
            }
            onClick={handleSavePositions}
          >
            保存统计岗位
          </Button>
          {positionsMsg && (
            <Alert
              type={positionsMsg.includes('已保存') ? 'success' : 'error'}
              showIcon
              message={positionsMsg}
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
