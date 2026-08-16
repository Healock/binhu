import type {
  QmfRegistrationRun,
  QmfRegistrationRunStatus,
  QmfRegistrationStepStatus,
  QmfTencentMarkerStatus,
} from '../api/client'

export const QMF_RUN_STATUS: Record<
  QmfRegistrationRunStatus,
  { label: string; color: string; terminal: boolean }
> = {
  prepared: { label: '等待确认', color: 'gold', terminal: false },
  executing: { label: '正在登记', color: 'processing', terminal: false },
  succeeded: { label: '登记成功', color: 'success', terminal: true },
  failed: { label: '已停止并冻结', color: 'error', terminal: true },
  uncertain: { label: '结果待人工核查', color: 'error', terminal: true },
  expired: { label: '准备已过期', color: 'default', terminal: true },
  superseded: { label: '已被新预演替代', color: 'default', terminal: true },
}

export const QMF_STEP_STATUS: Record<
  QmfRegistrationStepStatus,
  { label: string; color: string }
> = {
  pending: { label: '等待', color: 'default' },
  sending: { label: '进行中', color: 'processing' },
  succeeded: { label: '成功', color: 'success' },
  failed: { label: '失败并停止', color: 'error' },
  uncertain: { label: '结果不确定', color: 'error' },
}

export const QMF_MARKER_STATUS: Record<
  QmfTencentMarkerStatus,
  { label: string; color: string }
> = {
  not_started: { label: '腾讯标记待写入', color: 'warning' },
  writing: { label: '正在写入腾讯标记', color: 'processing' },
  succeeded: { label: '腾讯标记已完成', color: 'success' },
  pending: { label: '腾讯标记待人工重试', color: 'warning' },
  conflict: { label: '腾讯来源冲突，待人工重试', color: 'error' },
  failed: { label: '腾讯标记失败，待人工重试', color: 'error' },
}

const QMF_WRITE_STEP_KEYS = new Set([
  'upload_photo',
  'save_local_photo',
  'register_person',
  'complete_task',
])

const QMF_WRITE_PROGRESS = new Set(['sending', 'succeeded', 'uncertain'])

/**
 * Keep the recovery button visible for legacy runs whose API response did not
 * yet include the derived flag.  The step list is not trusted to authorize a
 * write: clicking still calls prepare, and the backend rechecks the run and
 * current source before doing anything external.
 */
export function qmfRunCanReprepare(run: QmfRegistrationRun | null | undefined) {
  if (!run || !['failed', 'uncertain'].includes(run.status)) return false
  if (run.can_reprepare) return true
  return !run.steps.some(
    step => QMF_WRITE_STEP_KEYS.has(step.key) && QMF_WRITE_PROGRESS.has(step.status),
  )
}

export function qmfRunIsPolling(run: QmfRegistrationRun | null | undefined) {
  return run?.status === 'executing' || run?.tencent_marker_status === 'writing'
}

export function canExecutePreparedQmfRun(
  run: QmfRegistrationRun | null | undefined,
  hasFreshPreview: boolean,
) {
  return Boolean(
    run?.status === 'prepared'
    && run.can_execute
    && hasFreshPreview
  )
}
