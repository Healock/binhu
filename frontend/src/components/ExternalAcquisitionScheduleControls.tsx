import { useEffect, useState } from 'react'
import { Alert, Button, Input, InputNumber, Switch, message } from 'antd'
import {
  getQmfConfig,
  getResidencePlatformConfig,
  updateQmfConfig,
  updateResidencePlatformConfig,
  type QmfConfig,
  type ResidencePlatformConfig,
} from '../api/client'

export default function ExternalAcquisitionScheduleControls() {
  const [qmf, setQmf] = useState<QmfConfig | null>(null)
  const [residence, setResidence] = useState<ResidencePlatformConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<'qmf' | 'residence' | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void Promise.all([getQmfConfig(), getResidencePlatformConfig()])
      .then(([qmfConfig, residenceConfig]) => {
        if (!active) return
        setQmf(qmfConfig)
        setResidence(residenceConfig)
      })
      .catch(reason => {
        if (active) setError(reason?.response?.data?.detail || '自动读取配置加载失败')
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  const saveQmf = async () => {
    if (!qmf) return
    setSaving('qmf')
    setError('')
    try {
      const next = await updateQmfConfig({
        registration_enabled: false,
        api_base_url: qmf.api_base_url,
        login_host: qmf.login_host,
        login_port: qmf.login_port,
        source_username: qmf.source_username,
        source_imei: qmf.source_imei,
        source_machine_uid: qmf.source_machine_uid,
        expected_station_code: qmf.expected_station_code,
        expected_station_name: qmf.expected_station_name,
        timeout_seconds: qmf.timeout_seconds,
        session_max_seconds: qmf.session_max_seconds,
        status_scan_enabled: qmf.status_scan_enabled,
        status_scan_time: qmf.status_scan_time,
      })
      setQmf(next)
      message.success('全民防每日只读扫描设置已保存')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '全民防扫描设置保存失败')
    } finally {
      setSaving(null)
    }
  }

  const saveResidence = async () => {
    if (!residence) return
    setSaving('residence')
    setError('')
    try {
      const next = await updateResidencePlatformConfig({
        enabled: residence.enabled,
        base_url: residence.base_url,
        login_community_ids: residence.login_community_ids,
        timeout_seconds: residence.timeout_seconds,
        full_scan_interval_minutes: residence.full_scan_interval_minutes,
      })
      setResidence(next)
      message.success('居住证自动查询设置已保存')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail?.message || reason?.response?.data?.detail || '居住证设置保存失败')
    } finally {
      setSaving(null)
    }
  }

  return <div className="grid gap-4">
    {error && <Alert type="error" showIcon message={error} />}
    {loading ? <div className="text-sm text-[var(--app-text-secondary)]">正在读取自动任务配置</div> : <>
      <section className="grid gap-3 rounded-md border border-[var(--app-border)] p-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><strong>全民防每日状态扫描</strong><div className="text-xs text-[var(--app-text-secondary)]">只读扫描新增或变化任务及长期未核对任务，不执行登记或反馈。</div></div>
          <Switch checked={Boolean(qmf?.status_scan_enabled)} onChange={value => setQmf(current => current ? { ...current, status_scan_enabled: value } : current)} />
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="grid gap-1 text-sm"><span>每日执行时间</span><Input type="time" value={qmf?.status_scan_time} onChange={event => setQmf(current => current ? { ...current, status_scan_time: event.target.value } : current)} /></label>
          <Button loading={saving === 'qmf'} onClick={() => void saveQmf()}>保存调度</Button>
          <span className="text-xs text-[var(--app-text-secondary)]">配置状态：{qmf?.configured ? '已就绪' : '未完成'}</span>
        </div>
      </section>
      <section className="grid gap-3 rounded-md border border-[var(--app-border)] p-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><strong>居住证全量只读查询</strong><div className="text-xs text-[var(--app-text-secondary)]">按已配置社区账号查询平台状态；不会修改居住证平台。</div></div>
          <Switch checked={Boolean(residence?.enabled)} onChange={value => setResidence(current => current ? { ...current, enabled: value } : current)} />
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="grid gap-1 text-sm"><span>全量查询间隔（分钟）</span><InputNumber min={5} max={1440} value={residence?.full_scan_interval_minutes} onChange={value => setResidence(current => current ? { ...current, full_scan_interval_minutes: Number(value || 30) } : current)} /></label>
          <Button loading={saving === 'residence'} onClick={() => void saveResidence()}>保存调度</Button>
          <span className="text-xs text-[var(--app-text-secondary)]">配置状态：{residence?.session_ready ? '已就绪' : '未完成'}</span>
        </div>
      </section>
    </>}
  </div>
}
