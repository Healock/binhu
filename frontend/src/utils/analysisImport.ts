// Only render the public message; never stringify structured response payloads.
export function analysisImportReason(value: unknown, fallback = '该行未提交，请重新导出最新待研判任务，核对决定、意见和当前阶段后重试。'): string {
  if (typeof value === 'string' && value.trim()) return value
  if (value && typeof value === 'object' && 'message' in value && typeof value.message === 'string') return value.message || fallback
  return fallback
}
