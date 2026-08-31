import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Empty, Input, Modal, Spin, Tabs, Tag, message } from 'antd'
import {
  EditOutlined,
  ReloadOutlined,
  SearchOutlined,
  UndoOutlined,
} from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useSearchParams } from 'react-router-dom'
import {
  apiErrorMessage,
  getHelpDocument,
  listHelpDocuments,
  resetHelpDocument,
  updateHelpDocument,
  type HelpDocument,
  type HelpDocumentSummary,
} from '../api/client'
import { PageHeader } from '../components/ui'


interface EditorDraft {
  title: string
  summary: string
  content_md: string
}

function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="help-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children, ...props }) => {
            const external = Boolean(href && /^(https?:)?\/\//i.test(href))
            return (
              <a
                {...props}
                href={href}
                target={external ? '_blank' : undefined}
                rel={external ? 'noreferrer noopener' : undefined}
              >
                {children}
              </a>
            )
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

function updatedAtLabel(value: string | null): string {
  if (!value) return ''
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`
  const parsed = new Date(normalized)
  if (Number.isNaN(parsed.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(parsed)
}

export default function HelpCenter() {
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedSlug = searchParams.get('doc') || ''
  const [documents, setDocuments] = useState<HelpDocumentSummary[]>([])
  const [document, setDocument] = useState<HelpDocument | null>(null)
  const [keyword, setKeyword] = useState('')
  const [listLoading, setListLoading] = useState(true)
  const [documentLoading, setDocumentLoading] = useState(false)
  const [error, setError] = useState('')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorTab, setEditorTab] = useState('edit')
  const [draft, setDraft] = useState<EditorDraft>({ title: '', summary: '', content_md: '' })
  const [saving, setSaving] = useState(false)
  const detailRequestId = useRef(0)

  const loadList = async (preferredSlug?: string) => {
    setListLoading(true)
    setError('')
    try {
      const result = await listHelpDocuments()
      setDocuments(result.data)
      const requested = preferredSlug || searchParams.get('doc') || ''
      const nextSlug = result.data.some(item => item.slug === requested)
        ? requested
        : result.data[0]?.slug || ''
      if (nextSlug && nextSlug !== searchParams.get('doc')) {
        setSearchParams({ doc: nextSlug }, { replace: true })
      }
    } catch (reason) {
      setError(apiErrorMessage(reason, '帮助文档读取失败，请稍后重试'))
    } finally {
      setListLoading(false)
    }
  }

  useEffect(() => {
    void loadList()
    // 首次加载由 URL 决定文档；后续 URL 变化只读取正文。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!selectedSlug) {
      setDocument(null)
      return
    }
    const requestId = ++detailRequestId.current
    setDocumentLoading(true)
    setError('')
    void getHelpDocument(selectedSlug)
      .then((result) => {
        if (requestId === detailRequestId.current) setDocument(result)
      })
      .catch((reason) => {
        if (requestId === detailRequestId.current) {
          setError(apiErrorMessage(reason, '帮助文档正文读取失败，请稍后重试'))
        }
      })
      .finally(() => {
        if (requestId === detailRequestId.current) setDocumentLoading(false)
      })
  }, [selectedSlug])

  const filteredDocuments = useMemo(() => {
    const normalized = keyword.trim().toLowerCase()
    if (!normalized) return documents
    return documents.filter(item => (
      `${item.title} ${item.category} ${item.summary}`.toLowerCase().includes(normalized)
    ))
  }, [documents, keyword])

  const groups = useMemo(() => {
    const grouped = new Map<string, HelpDocumentSummary[]>()
    for (const item of filteredDocuments) {
      const entries = grouped.get(item.category) || []
      entries.push(item)
      grouped.set(item.category, entries)
    }
    return [...grouped.entries()]
  }, [filteredDocuments])

  const openEditor = () => {
    if (!document?.can_edit) return
    setDraft({
      title: document.title,
      summary: document.summary,
      content_md: document.content_md,
    })
    setEditorTab('edit')
    setEditorOpen(true)
  }

  const applyDocument = (next: HelpDocument) => {
    setDocument(next)
    setDocuments(current => current.map(item => (
      item.slug === next.slug ? { ...item, ...next } : item
    )))
  }

  const saveDocument = async () => {
    if (!document) return
    setSaving(true)
    try {
      const next = await updateHelpDocument(document.slug, {
        ...draft,
        expected_revision: document.revision,
      })
      applyDocument(next)
      setEditorOpen(false)
      message.success('帮助文档已更新')
    } catch (reason) {
      message.error(apiErrorMessage(reason, '帮助文档保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const confirmReset = () => {
    if (!document?.can_edit) return
    Modal.confirm({
      title: '恢复内置帮助文档？',
      content: '当前线上修改会被随版本发布的 Markdown 内容替换，此操作会留下审计记录。',
      okText: '恢复内置版本',
      cancelText: '取消',
      okButtonProps: { danger: true },
      async onOk() {
        try {
          const next = await resetHelpDocument(document.slug, document.revision)
          applyDocument(next)
          message.success('已恢复内置帮助文档')
        } catch (reason) {
          message.error(apiErrorMessage(reason, '恢复内置帮助文档失败'))
          throw reason
        }
      },
    })
  }

  return (
    <div className="help-center-page">
      <PageHeader
        title="帮助中心"
        description="查看平台各项功能的用途、操作步骤和常见问题。"
        actions={(
          <Button icon={<ReloadOutlined />} onClick={() => void loadList(selectedSlug)} loading={listLoading}>
            刷新文档
          </Button>
        )}
      />

      {error && <Alert type="error" showIcon message={error} />}

      <div className="help-center-layout">
        <aside className="app-card help-center-sidebar">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
            placeholder="搜索标题或功能"
          />
          <div className="help-center-catalog" aria-label="帮助文档目录">
            {listLoading ? (
              <div className="help-center-loading"><Spin size="small" /> 正在读取目录…</div>
            ) : groups.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的帮助文档" />
            ) : groups.map(([category, items]) => (
              <section key={category} className="help-center-category">
                <h2>{category}</h2>
                <div className="help-center-category__items">
                  {items.map(item => (
                    <button
                      key={item.slug}
                      type="button"
                      className={item.slug === selectedSlug ? 'is-active' : ''}
                      onClick={() => setSearchParams({ doc: item.slug })}
                    >
                      <span>{item.title}</span>
                      <small>{item.summary}</small>
                    </button>
                  ))}
                </div>
              </section>
            ))}
          </div>
        </aside>

        <main className="app-card help-center-document">
          {documentLoading ? (
            <div className="help-center-loading"><Spin /> 正在读取文档…</div>
          ) : !document ? (
            <Empty description="请选择一篇帮助文档" />
          ) : (
            <>
              <header className="help-center-document__header">
                <div>
                  <div className="help-center-document__meta">
                    <Tag color="blue">{document.category}</Tag>
                    {document.is_customized && <Tag color="gold">已在线修改</Tag>}
                    {updatedAtLabel(document.updated_at) && (
                      <span>最近更新：{updatedAtLabel(document.updated_at)}</span>
                    )}
                  </div>
                  <p>{document.summary}</p>
                </div>
                {document.can_edit && (
                  <div className="help-center-document__actions">
                    {document.is_customized && (
                      <Button icon={<UndoOutlined />} onClick={confirmReset}>恢复内置版本</Button>
                    )}
                    <Button type="primary" icon={<EditOutlined />} onClick={openEditor}>
                      编辑 Markdown
                    </Button>
                  </div>
                )}
              </header>
              <MarkdownContent content={document.content_md} />
            </>
          )}
        </main>
      </div>

      <Modal
        title={document ? `编辑：${document.title}` : '编辑帮助文档'}
        open={editorOpen}
        width={1100}
        okText="保存并发布"
        cancelText="取消"
        confirmLoading={saving}
        onOk={() => void saveDocument()}
        onCancel={() => !saving && setEditorOpen(false)}
        destroyOnHidden
      >
        <div className="help-editor-fields">
          <label>
            <span>标题</span>
            <Input
              value={draft.title}
              maxLength={160}
              showCount
              onChange={event => setDraft(current => ({ ...current, title: event.target.value }))}
            />
          </label>
          <label>
            <span>摘要</span>
            <Input.TextArea
              value={draft.summary}
              maxLength={500}
              showCount
              autoSize={{ minRows: 2, maxRows: 4 }}
              onChange={event => setDraft(current => ({ ...current, summary: event.target.value }))}
            />
          </label>
        </div>
        <Tabs
          activeKey={editorTab}
          onChange={setEditorTab}
          items={[
            {
              key: 'edit',
              label: 'Markdown 编辑',
              children: (
                <Input.TextArea
                  className="help-editor-textarea"
                  value={draft.content_md}
                  maxLength={200000}
                  showCount
                  onChange={event => setDraft(current => ({ ...current, content_md: event.target.value }))}
                />
              ),
            },
            {
              key: 'preview',
              label: '预览',
              children: (
                <div className="help-editor-preview">
                  <MarkdownContent content={draft.content_md} />
                </div>
              ),
            },
          ]}
        />
      </Modal>
    </div>
  )
}
