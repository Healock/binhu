import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Input,
  InputNumber,
  Radio,
  Select,
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
import { useAuth } from '../context/AuthContext'
import {
  formatCountdown,
  getRemainingTime,
  getServerOffset,
} from '../utils/countdown'
import {
  DEFAULT_SUMMARY_POSITIONS,
  PERSONNEL_POSITIONS,
  RENTAL_PERSONNEL_POSITIONS,
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

type MaintenanceMode = 'off' | 'immediate' | 'scheduled'

function formatDateTimeInput(value: string | undefined, timezone: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(date)
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`
}

function parseDateTimeInput(value: string, timezone: string): string {
  if (!value) return ''
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/)
  if (!match) return ''
  const target = Date.UTC(
    Number(match[1]), Number(match[2]) - 1, Number(match[3]),
    Number(match[4]), Number(match[5]),
  )
  let candidate = target
  for (let index = 0; index < 4; index += 1) {
    const rendered = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).formatToParts(new Date(candidate))
    const values = Object.fromEntries(rendered.map(part => [part.type, part.value]))
    const renderedTarget = Date.UTC(
      Number(values.year), Number(values.month) - 1, Number(values.day),
      Number(values.hour), Number(values.minute),
    )
    const correction = target - renderedTarget
    candidate += correction
    if (correction === 0) break
  }
  return new Date(candidate).toISOString().replace('.000Z', 'Z')
}

export default function SystemSettings() {
  const { setSystemTimezone } = useAuth()
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
  const [visitPositions, setVisitPositions] = useState<PersonnelPosition[]>(
    [...DEFAULT_SUMMARY_POSITIONS],
  )
  const [weekendDutyPositions, setWeekendDutyPositions] = useState<
    PersonnelPosition[]
  >([...DEFAULT_SUMMARY_POSITIONS])
  const [savingPositions, setSavingPositions] = useState(false)
  const [positionsMsg, setPositionsMsg] = useState('')
  const [clock, setClock] = useState(Date.now())
  const [idleMinutes, setIdleMinutes] = useState(30)
  const [savingIdle, setSavingIdle] = useState(false)
  const [idleMsg, setIdleMsg] = useState('')
  const [onlineWritebackEnabled, setOnlineWritebackEnabled] = useState(false)
  const [savingWriteback, setSavingWriteback] = useState(false)
  const [writebackMsg, setWritebackMsg] = useState('')
  const [maintenanceMode, setMaintenanceMode] = useState<MaintenanceMode>('off')
  const [maintenanceStartAt, setMaintenanceStartAt] = useState('')
  const [maintenanceEndAt, setMaintenanceEndAt] = useState('')
  const [maintenanceMessage, setMaintenanceMessage] = useState('平台正在维护中，请稍后再试')
  const [savingMaintenance, setSavingMaintenance] = useState(false)
  const [maintenanceMsg, setMaintenanceMsg] = useState('')

  useEffect(() => {
    Promise.all([getSystemConfig(), getSyncSchedule()])
      .then(([config, currentSchedule]) => {
        setTimezone(config.timezone || 'Asia/Shanghai')
        const configuredTimezone = config.timezone || 'Asia/Shanghai'
        const configuredStartAt = formatDateTimeInput(config.maintenance_start_at, configuredTimezone)
        const configuredEndAt = formatDateTimeInput(config.maintenance_end_at, configuredTimezone)
        setMaintenanceMode(
          String(config.maintenance_enabled || '0') !== '1'
            ? 'off'
            : configuredStartAt
              ? 'scheduled'
              : 'immediate',
        )
        setMaintenanceStartAt(configuredStartAt)
        setMaintenanceEndAt(configuredEndAt)
        setMaintenanceMessage(config.maintenance_message || '平台正在维护中，请稍后再试')
        setIdleMinutes(Number(config.session_idle_minutes || 30))
        setOnlineWritebackEnabled(
          String(config.online_writeback_enabled || '0') === '1',
        )
        const configuredVisitPositions = parseSummaryPositions(
          config.visit_summary_positions,
        ).filter(position => position !== '自购房')
        setVisitPositions(
          configuredVisitPositions.length
            ? configuredVisitPositions
            : [...DEFAULT_SUMMARY_POSITIONS],
        )
        setWeekendDutyPositions(
          parseSummaryPositions(config.weekend_duty_positions),
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
      setSystemTimezone(timezone)
      setTimezoneMsg('保存成功，平台时间显示已更新')
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
    if (
      !visitPositions.length
      || !weekendDutyPositions.length
    ) {
      setPositionsMsg('每项岗位范围都至少选择一个岗位')
      return
    }
    setSavingPositions(true)
    setPositionsMsg('')
    try {
      await updateSystemConfig({
        visit_summary_positions: JSON.stringify(visitPositions),
        weekend_duty_positions: JSON.stringify(weekendDutyPositions),
      })
      setPositionsMsg('岗位范围已保存，重新打开相关页面后生效')
    } catch (error: any) {
      setPositionsMsg(error?.response?.data?.detail || '保存失败')
    } finally {
      setSavingPositions(false)
    }
  }

  const handleSaveIdle = async () => {
    setSavingIdle(true)
    setIdleMsg('')
    try {
      await updateSystemConfig({ session_idle_minutes: String(idleMinutes) })
      setIdleMsg('空闲退出时间已保存，并立即对现有登录生效')
    } catch (error: any) {
      setIdleMsg(error?.response?.data?.detail || '保存失败')
    } finally {
      setSavingIdle(false)
    }
  }

  const handleSaveWriteback = async () => {
    setSavingWriteback(true)
    setWritebackMsg('')
    try {
      await updateSystemConfig({
        online_writeback_enabled: onlineWritebackEnabled ? '1' : '0',
      })
      setWritebackMsg(
        onlineWritebackEnabled
          ? '在线回写已启用'
          : '在线回写已暂停，数据查询仍可正常使用',
      )
    } catch (error: any) {
      setWritebackMsg(error?.response?.data?.detail || '保存失败')
    } finally {
      setSavingWriteback(false)
    }
  }

  const handleSaveMaintenance = async () => {
    setSavingMaintenance(true)
    setMaintenanceMsg('')
    try {
      if (maintenanceMode === 'scheduled' && !maintenanceStartAt) {
        setMaintenanceMsg('预约维护需要填写开始时间')
        return
      }
      const startAt = maintenanceMode === 'scheduled'
        ? parseDateTimeInput(maintenanceStartAt, timezone)
        : ''
      const endAt = maintenanceMode === 'scheduled'
        ? parseDateTimeInput(maintenanceEndAt, timezone)
        : ''
      if (startAt && endAt && new Date(endAt) <= new Date(startAt)) {
        setMaintenanceMsg('维护结束时间必须晚于开始时间')
        return
      }
      await updateSystemConfig({
        maintenance_enabled: maintenanceMode === 'off' ? '0' : '1',
        maintenance_start_at: startAt,
        maintenance_end_at: endAt,
        maintenance_message: maintenanceMessage.trim(),
      })
      setMaintenanceMsg(
        maintenanceMode !== 'off'
          ? (maintenanceMode === 'scheduled' ? '维护预约已保存' : '维护模式已启用，普通用户将立即看到维护提示')
          : '维护模式已关闭，平台恢复正常访问',
      )
    } catch (error: any) {
      setMaintenanceMsg(error?.response?.data?.detail || '维护设置保存失败')
    } finally {
      setSavingMaintenance(false)
    }
  }

  return (
    <div className="system-settings-page settings-stack">
      <Panel
        className="maintenance-settings-panel"
        title="平台维护模式"
        description="仅超级管理员可以配置；维护期间普通用户不能登录或访问业务接口。"
      >
        <div className="maintenance-settings-panel__content settings-stack">
          <Alert
            type="warning"
            showIcon
            message="底层维护仍使用服务器维护页"
            description="数据库迁移、容器重建等操作继续先切换 Nginx 维护页；这里的维护模式适合预约业务停用，不会修改服务器系统时钟。"
          />
          <div className="maintenance-settings-panel__state settings-field rounded-lg border border-[var(--app-border)] px-4 py-3">
            <div className="mb-1.5 text-sm font-medium text-[var(--app-text-strong)]">维护状态</div>
            <Radio.Group
              className="maintenance-mode-options"
              value={maintenanceMode}
              onChange={event => {
                const nextMode = event.target.value as MaintenanceMode
                setMaintenanceMode(nextMode)
                if (nextMode !== 'scheduled') {
                  setMaintenanceStartAt('')
                  setMaintenanceEndAt('')
                }
              }}
              optionType="button"
              buttonStyle="solid"
              disabled={loading || savingMaintenance}
              options={[
                { label: '关闭维护', value: 'off' },
                { label: '立即维护', value: 'immediate' },
                { label: '预约维护', value: 'scheduled' },
              ]}
            />
            <p className="settings-field__hint text-xs text-[var(--app-text-secondary)]">
              {maintenanceMode === 'off'
                ? '平台正常开放。'
                : maintenanceMode === 'immediate'
                  ? '保存后立即进入维护，普通用户将无法登录或访问业务接口。'
                  : '填写开始时间后保存，平台会在指定时间自动进入维护。'}
            </p>
          </div>
          {maintenanceMode === 'scheduled' && (
            <div className="settings-field-grid">
              <label className="settings-field text-sm text-[var(--app-text-strong)]">
                <span className="settings-field__label font-medium">开始时间（{timezone}）</span>
                <Input
                  type="datetime-local"
                  value={maintenanceStartAt}
                  onChange={event => setMaintenanceStartAt(event.target.value)}
                  disabled={loading || savingMaintenance}
                />
              </label>
              <label className="settings-field text-sm text-[var(--app-text-strong)]">
                <span className="settings-field__label font-medium">结束时间（{timezone}，可选）</span>
                <Input
                  type="datetime-local"
                  value={maintenanceEndAt}
                  onChange={event => setMaintenanceEndAt(event.target.value)}
                  disabled={loading || savingMaintenance}
                />
              </label>
            </div>
          )}
          <label className="settings-field settings-field--counted text-sm text-[var(--app-text-strong)]">
            <span className="settings-field__label font-medium">维护说明</span>
            <Input.TextArea
              value={maintenanceMessage}
              onChange={event => setMaintenanceMessage(event.target.value)}
              maxLength={500}
              showCount
              autoSize={{ minRows: 2, maxRows: 4 }}
              placeholder="例如：系统升级，预计稍后恢复"
              disabled={loading}
            />
          </label>
          <div className="settings-actions">
            <Button
              type="primary"
              loading={savingMaintenance}
              disabled={loading}
              onClick={handleSaveMaintenance}
            >
              {maintenanceMode === 'off'
                ? '保存并关闭维护'
                : maintenanceMode === 'scheduled'
                  ? '保存预约维护'
                  : '立即启用维护'}
            </Button>
          </div>
          {maintenanceMsg && (
            <Alert
              type={maintenanceMsg.includes('失败') || maintenanceMsg.includes('必须') ? 'error' : 'success'}
              showIcon
              message={maintenanceMsg}
            />
          )}
        </div>
      </Panel>

      <Panel
        title="自动同步"
        description="按固定间隔读取腾讯文档；修改设置后会重新开始倒计时"
      >
        <div className="flex flex-col gap-5">
          <div className="grid gap-5 md:grid-cols-2">
            <div>
              <div className="mb-2 text-sm font-medium text-slate-700">启用状态</div>
              <div className="flex min-h-9 items-center gap-3">
                <Switch
                  checked={enabled}
                  onChange={setEnabled}
                  loading={loading}
                />
                <span className="text-sm text-slate-600">
                  {enabled ? '已开启自动同步' : '已关闭自动同步'}
                </span>
              </div>
            </div>

            <div className="settings-field">
              <label className="settings-field__label text-sm font-medium text-slate-700">
                同步间隔
              </label>
              <div className="flex flex-wrap gap-3">
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
              </div>
              <p className="settings-field__hint text-xs text-slate-500">
                自定义范围为 5 分钟至 7 天。
              </p>
            </div>
          </div>

          <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
            <Descriptions
              size="small"
              colon={false}
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
          </div>

          <div className="flex justify-end">
            <Button
              type="primary"
              onClick={handleSaveSchedule}
              loading={savingSchedule}
              disabled={loading || interval < 5 || interval > 10080}
            >
              保存自动同步设置
            </Button>
          </div>
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
        title="腾讯文档在线回写"
        description="控制在线数据查询页是否允许把修改、新增和删除直接写回腾讯表格"
      >
        <div className="flex flex-col gap-4">
          <Alert
            type="warning"
            showIcon
            message="关闭开关不会影响查询和正常同步"
            description="开启后，符合岗位和社区范围的账号才能编辑；修改结果要等下一次正常同步后才进入业务库、日报和汇总。"
          />
          <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg border border-[var(--app-border)] px-4 py-3">
            <div>
              <div className="text-sm font-medium text-[var(--app-text-strong)]">
                允许平台回写腾讯文档
              </div>
              <div className="mt-1 text-xs text-[var(--app-text-secondary)]">
                发生异常时可立即关闭，已经登录的账号下一次写操作会立即受限。
              </div>
            </div>
            <Switch
              checked={onlineWritebackEnabled}
              onChange={setOnlineWritebackEnabled}
              loading={loading}
            />
          </div>
          <div className="flex justify-end">
            <Button
              type="primary"
              loading={savingWriteback}
              disabled={loading}
              onClick={handleSaveWriteback}
            >
              保存在线回写设置
            </Button>
          </div>
          {writebackMsg && (
            <Alert
              type={writebackMsg.includes('已') ? 'success' : 'error'}
              showIcon
              message={writebackMsg}
            />
          )}
        </div>
      </Panel>

      <Panel
        title="登录安全"
        description="连续一段时间没有页面跳转、查询或保存操作后，需要重新登录"
      >
        <div className="flex flex-col gap-4">
          <div className="settings-field">
            <label className="settings-field__label text-sm font-medium text-slate-700">
              空闲退出时间
            </label>
            <InputNumber
              min={5}
              max={1440}
              value={idleMinutes}
              onChange={value => setIdleMinutes(value || 30)}
              addonAfter="分钟"
              className="w-56"
            />
            <p className="settings-field__hint text-xs text-slate-500">
              可设置 5 分钟至 24 小时；到期前 2 分钟会弹出提醒。
            </p>
          </div>
          <div className="flex justify-end">
            <Button
              type="primary"
              loading={savingIdle}
              disabled={idleMinutes < 5 || idleMinutes > 1440}
              onClick={handleSaveIdle}
            >
              保存登录安全设置
            </Button>
          </div>
          {idleMsg && (
            <Alert
              type={idleMsg.includes('已保存') ? 'success' : 'error'}
              showIcon
              message={idleMsg}
            />
          )}
        </div>
      </Panel>

      <Panel
        title="岗位范围"
        description="配置参与走访汇总和双休日备勤的岗位；在线汇总固定统计有社区部门的组长和组员"
      >
        <div className="flex flex-col gap-5">
          <div className="grid gap-5 md:grid-cols-2">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">
                出租房走访汇总
              </label>
              <Select<PersonnelPosition[]>
                mode="multiple"
                value={visitPositions}
                onChange={setVisitPositions}
                options={RENTAL_PERSONNEL_POSITIONS.map(position => ({
                  value: position,
                  label: position,
                }))}
                placeholder="选择参与出租房走访汇总的岗位"
                className="w-full"
                maxTagCount="responsive"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">
                双休日备勤
              </label>
              <Select<PersonnelPosition[]>
                mode="multiple"
                value={weekendDutyPositions}
                onChange={setWeekendDutyPositions}
                options={PERSONNEL_POSITIONS.map(position => ({
                  value: position,
                  label: position,
                }))}
                placeholder="选择需要每周排班的岗位"
                className="w-full"
                maxTagCount="responsive"
              />
            </div>
          </div>
          <Alert
            type="info"
            showIcon
            message="在线汇总口径已固定"
            description="在线数据只统计人员管理中有有效社区部门的组长和组员。出租房走访和双休日备勤仍可分别配置；自购房汇总固定统计“自购房”岗位。"
          />
          <div className="flex justify-end">
            <Button
              type="primary"
              loading={savingPositions}
              disabled={
                loading
                || !visitPositions.length
                || !weekendDutyPositions.length
              }
              onClick={handleSavePositions}
            >
              保存岗位范围
            </Button>
          </div>
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
        <div className="flex flex-col gap-4">
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
          <div className="flex justify-end">
            <Button
              type="primary"
              onClick={handleSaveTimezone}
              loading={savingTimezone}
            >
              保存时区
            </Button>
          </div>
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
