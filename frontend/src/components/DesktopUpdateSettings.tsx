import { useState } from 'react'
import { Alert, Button, Tag } from 'antd'
import {
  CheckCircleOutlined,
  DownloadOutlined,
  LoadingOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useClientUpdateStatus } from '../desktop/useClientUpdateStatus'
import { Panel } from './ui'

export default function DesktopUpdateSettings() {
  const { bridge, status, setStatus } = useClientUpdateStatus()
  const [actionError, setActionError] = useState('')

  if (!bridge) {
    return (
      <Panel title="应用更新" description="桌面客户端会在启动时自动检查更新。">
        <Alert type="info" showIcon message="当前浏览器无需使用客户端更新功能。" />
      </Panel>
    )
  }

  const busy = status?.state === 'checking'
    || status?.state === 'downloading'
    || status?.state === 'applying'
  const currentVersion = status?.currentVersion || __APP_VERSION__
  const available = status?.state === 'available'
    || status?.state === 'downloading'
    || status?.state === 'ready'
    || status?.state === 'applying'
  const android = status?.platform === 'android'

  const runAction = async () => {
    setActionError('')
    try {
      const next = status?.state === 'available'
        ? await bridge.downloadUpdate()
        : status?.state === 'ready'
          ? await bridge.restartAndApply()
          : await bridge.checkForUpdates()
      setStatus(next)
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '更新操作失败，请稍后重试')
    }
  }

  const label = status?.state === 'checking'
    ? '正在检查更新…'
    : status?.state === 'downloading'
      ? `正在下载 ${status.progress ?? 0}%`
      : status?.state === 'ready'
        ? android ? '安装更新' : '重启并应用更新'
        : status?.state === 'applying'
          ? android ? '正在打开安装界面…' : '正在应用更新…'
          : available
            ? `下载 v${status?.availableVersion || '新版本'}`
            : '检查更新'

  const icon = busy
    ? <LoadingOutlined spin />
    : status?.state === 'ready'
      ? <PoweroffOutlined />
      : status?.state === 'error'
        ? <ReloadOutlined />
        : available
          ? <DownloadOutlined />
          : <ReloadOutlined />

  return (
    <Panel title="应用更新" description="客户端会在每次启动时自动检查一次；你也可以在这里手动检查。">
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--app-border)] bg-[var(--app-bg)] px-4 py-3">
          <div>
            <div className="text-sm font-medium text-[var(--app-text-strong)]">当前客户端版本</div>
            <div className="mt-1 text-lg font-semibold text-[var(--app-primary)]">v{currentVersion}</div>
          </div>
          <div className="flex items-center gap-2">
            {status?.state === 'idle' && <Tag icon={<CheckCircleOutlined />} color="success">已是最新版本</Tag>}
            {status?.state === 'checking' && <Tag color="processing">检查中</Tag>}
            {available && status?.availableVersion && <Tag color="warning">发现 v{status.availableVersion}</Tag>}
            {status?.state === 'error' && <Tag icon={<WarningOutlined />} color="error">检查失败</Tag>}
            <Button type={available ? 'primary' : 'default'} icon={icon} loading={false} disabled={busy} onClick={() => void runAction()}>
              {label}
            </Button>
          </div>
        </div>
        {status?.state === 'error' && status.error && <Alert type="error" showIcon message={status.error} />}
        {actionError && <Alert type="error" showIcon message={actionError} />}
        {status?.state === 'downloading' && <Alert type="info" showIcon message={`正在下载新版本，当前进度 ${status.progress ?? 0}%。`} />}
        {status?.state === 'ready' && (
          <Alert
            type="success"
            showIcon
            message={android
              ? status.requiresInstallPermission
                ? '更新包已通过校验。点击“安装更新”，按系统提示允许本应用安装未知应用。'
                : '更新包已通过校验。点击“安装更新”后，请在 Android 系统界面确认覆盖安装。'
              : '更新包已准备好，点击按钮后客户端会重启并应用更新。'}
          />
        )}
      </div>
    </Panel>
  )
}
