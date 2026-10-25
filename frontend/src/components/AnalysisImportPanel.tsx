import { useState } from 'react'
import { Alert, Button, Upload, message } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { importMobileTaskAnalysis } from '../api/client'
import { analysisImportReason } from '../utils/analysisImport'

type AnalysisImportResult = Awaited<ReturnType<typeof importMobileTaskAnalysis>> & { error?: string }

export default function AnalysisImportPanel() {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<AnalysisImportResult | null>(null)

  const importFile = async (file: File) => {
    setBusy(true)
    setResult(null)
    try {
      const value = await importMobileTaskAnalysis(file)
      setResult(value)
      if (value.failed_count) message.warning(`已导入 ${value.success_count} 条，${value.failed_count} 条需要处理`)
      else if (value.success_count > 0) message.success(`已导入 ${value.success_count} 条研判结果`)
      else message.warning('没有导入任何研判结果，请填写本次研判结果和研判意见后重试')
    } catch (reason: any) {
      let detail = reason?.response?.data?.detail
      if (reason?.response?.data instanceof Blob) {
        try { detail = JSON.parse(await reason.response.data.text())?.detail } catch { detail = '' }
      }
      setResult({ success_count: 0, failed_count: 0, success: [], failed: [], error: analysisImportReason(detail, '文件导入未完成。请检查文件格式；网络中断时先核对任务状态，再重新导出待处理任务。') })
    } finally {
      setBusy(false)
    }
  }

  return <div className="grid gap-3">
    <Alert type="info" showIcon message="导入结构化研判结果" description="仅接受平台导出的待研判工作簿；服务端逐行校验任务 ID、版本、哈希、流程版本与当前阶段，导入不创建新任务。" />
    <Upload accept=".xlsx" showUploadList={false} disabled={busy} beforeUpload={file => { void importFile(file); return false }}>
      <Button icon={<UploadOutlined />} loading={busy}>选择研判结果工作簿</Button>
    </Upload>
    {result && <section role="region" aria-label="研判导入结果" className="grid gap-3">
      {result.error ? <Alert type="error" showIcon message="导入未完成" description={result.error} /> : result.failed_count > 0 ? <>
        <Alert type="warning" showIcon message={`成功 ${result.success_count} 条 · 需要处理 ${result.failed_count} 条`} description="请修正失败行后重新导入；版本冲突时重新导出最新待研判任务。" />
        <div className="grid max-h-72 gap-2 overflow-y-auto" role="list" aria-label="研判导入问题明细">
          {result.failed.map(item => <div key={item.row} role="listitem" className="flex gap-3 rounded border border-[var(--app-border)] px-3 py-2 text-sm"><strong>第 {item.row} 行</strong><span>{analysisImportReason(item.reason)}</span></div>)}
        </div>
      </> : <Alert type={result.success_count > 0 ? 'success' : 'info'} showIcon message={result.success_count > 0 ? '研判结果已导入' : '没有导入任何研判结果'} description={result.success_count > 0 ? '可前往研判工作台核对最新状态。' : '请在“本次研判结果”和“研判意见”列填写内容后重试。'} />}
    </section>}
  </div>
}
