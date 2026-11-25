import { useState, type ReactNode } from 'react'
import { Alert, Button, Upload } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import type { RcFile } from 'antd/es/upload/interface'
import {
  getVisitImportIssues,
  importPeaceCode,
  uploadStarRating,
  uploadVisitDetail,
  type VisitImportResult,
} from '../api/client'
import { Panel } from './ui'

function WorkbookImport<Result>({ title, description, importFile, renderResult }: {
  title: string
  description: string
  importFile: (file: File) => Promise<Result>
  renderResult: (result: Result) => ReactNode
}) {
  const [file, setFile] = useState<RcFile | null>(null)
  const [result, setResult] = useState<Result | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!file || busy) return
    setBusy(true)
    setError('')
    setResult(null)
    try {
      setResult(await importFile(file))
      setFile(null)
    } catch (reason: any) {
      const detail = reason?.response?.data?.detail
      const timedOut = ['ECONNABORTED', 'ETIMEDOUT'].includes(reason?.code)
      setError(typeof detail === 'string' ? detail : timedOut
        ? '导入处理超时，请先到对应汇总确认日期数据，再决定是否重试。'
        : '导入未完成，请检查文件后重试。')
    } finally {
      setBusy(false)
    }
  }

  return <Panel title={title} description={description}>
    <div className="grid min-w-0 gap-3">
      <Upload
        accept=".xlsx"
        disabled={busy}
        maxCount={1}
        fileList={file ? [{ uid: file.uid, name: file.name }] : []}
        beforeUpload={next => {
          setFile(null)
          setResult(null)
          setError('')
          if (!/\.xlsx$/i.test(next.name) || next.size === 0 || next.size > 20 * 1024 * 1024) {
            setError('请选择不超过 20 MB 的非空 XLSX 文件。')
            return Upload.LIST_IGNORE
          }
          setFile(next)
          return false
        }}
        onRemove={() => {
          setFile(null)
          setError('')
        }}
      >
        <Button icon={<UploadOutlined />} disabled={busy}>选择 XLSX 文件</Button>
      </Upload>
      <Button type="primary" className="justify-self-start" disabled={!file} loading={busy} onClick={() => void submit()}>
        确认导入{title}
      </Button>
      {error && <Alert showIcon type="error" message={error} />}
      {result !== null && renderResult(result)}
    </div>
  </Panel>
}

function VisitImportFeedback({ result }: { result: VisitImportResult }) {
  const [issues, setIssues] = useState(result.issues)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadPage = async (page: number) => {
    setLoading(true)
    setError('')
    try {
      setIssues(await getVisitImportIssues(result.batch_id, page))
    } catch {
      setError('问题清单读取失败，请重试。')
    } finally {
      setLoading(false)
    }
  }

  return <div className="grid gap-3">
    <Alert
      showIcon
      type={result.status === 'failed' ? 'error' : result.status === 'partial' ? 'warning' : 'success'}
      message={result.message}
      description={`批次 ${result.batch_id} · 新增 ${result.inserted_rows} · 更新 ${result.updated_rows} · 无变化 ${result.unchanged_rows} · 忽略 ${result.ignored_rows} · 错误 ${result.error_count} · 警告 ${result.warning_count}`}
    />
    {issues.total > 0 && <div className="grid gap-2">
      <div>导入问题（{issues.total} 条）</div>
      <ul className="list-disc ps-5">{issues.data.map(issue => <li key={issue.id}>第 {issue.row_number} 行 · {issue.message}</li>)}</ul>
      {issues.total > issues.page_size && <div className="flex flex-wrap items-center gap-2">
        <Button disabled={loading || issues.page <= 1} onClick={() => void loadPage(issues.page - 1)}>上一页</Button>
        <span>第 {issues.page} 页</span>
        <Button disabled={loading || issues.page * issues.page_size >= issues.total} onClick={() => void loadPage(issues.page + 1)}>下一页</Button>
      </div>}
      {error && <Alert showIcon type="error" message={error} />}
    </div>}
  </div>
}

export function VisitWorkbookImports() {
  return <>
    <WorkbookImport
      title="走访明细"
      description="使用来源导出的走访明细 XLSX。导入后可查看批次结果及逐行问题。"
      importFile={uploadVisitDetail}
      renderResult={result => <VisitImportFeedback key={result.batch_id} result={result} />}
    />
    <WorkbookImport
      title="星级评定"
      description="星级评定须匹配已有走访。请先导入走访明细，再导入星级评定 XLSX。"
      importFile={uploadStarRating}
      renderResult={result => <VisitImportFeedback key={result.batch_id} result={result} />}
    />
  </>
}

export function PeaceCodeWorkbookImport() {
  return <WorkbookImport
    title="平安码"
    description="请选择明细 XLSX。首张工作表的首行应有身份证号（文本格式）、比对时间、采集位置、人员类型和机构名称。导入后，文件所含日期使用新汇总，请上传这些日期的完整明细。"
    importFile={importPeaceCode}
    renderResult={result => <Alert
      showIcon
      type={result.status === 'warning' ? 'warning' : 'success'}
      message={result.unclassified_count ? '平安码已导入，部分地点待分类' : '平安码已导入'}
      description={`批次 ${result.run_id} · ${result.start_date} 至 ${result.end_date}，共 ${result.days} 个有数据的日期 · 原始 ${result.raw_count} 条 · 去重后 ${result.valid_count} 条 · 重复 ${result.duplicate_count} 条 · 地点待分类 ${result.unclassified_count} 条`}
    />}
  />
}
