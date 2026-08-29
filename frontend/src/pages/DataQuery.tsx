import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Pagination,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  message,
} from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  HistoryOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import {
  createQuerySourceRow,
  deleteQuerySourceRow,
  formatUTCTime,
  getQueryDataVersion,
  getQuerySourceRows,
  getQueryTypes,
  getQueryWritebackAudit,
  queryData,
  updateQuerySourceCell,
  type QueryColumnMeta,
  type QueryDataRow,
  type QueryDependentOptions,
  type QuerySourceRow,
  type QueryWritebackAudit,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { PageHeader } from '../components/ui'
import { QuerySpreadsheet } from '../components/QuerySpreadsheet'
import {
  buildQueryAuditChanges,
  isQueryDraftTouched,
  loadCompleteQueryResult,
  missingQueryDraftFields,
  saveChangedSourceFields,
  sourceToDisplay,
  type QueryDisplayRow as DisplayRow,
} from '../utils/queryGrid'
import {
  buildQuerySheetRequestFilters,
  isQuerySheetFullscreen,
  queryInspectorMismatch,
  queryInspectorOptions,
  toggleQuerySheetFullscreen,
  type QuerySheetCellChange,
  type QuerySheetFilterCriteria,
} from '../utils/querySpreadsheet'

const MOBILE_CARD_PAGE_SIZE = 50

function errorText(error: any, fallback: string): string {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail.message === 'string') return detail.message
  return fallback
}

function errorStatus(error: any): number {
  return Number(error?.response?.status || 0)
}

export default function DataQuery() {
  const { user, systemTimezone } = useAuth()
  const [types, setTypes] = useState<string[]>([])
  const [selectedType, setSelectedType] = useState('全链条')
  const [source, setSource] = useState<'online' | 'archive'>('online')
  const [searchInput, setSearchInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [sortBy, setSortBy] = useState<string>()
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc')
  const [rows, setRows] = useState<QueryDataRow[]>([])
  const [columns, setColumns] = useState<string[]>([])
  const [columnMeta, setColumnMeta] = useState<QueryColumnMeta[]>([])
  const [dependentOptions, setDependentOptions] = useState<QueryDependentOptions | undefined>()
  const [total, setTotal] = useState(0)
  const [mobilePage, setMobilePage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [scopeMessage, setScopeMessage] = useState('')
  const [rowManageMessage, setRowManageMessage] = useState('')
  const [sourceReady, setSourceReady] = useState(false)
  const [writebackEnabled, setWritebackEnabled] = useState(false)
  const [canAdd, setCanAdd] = useState(false)
  const [requiredFields, setRequiredFields] = useState<string[]>([])
  const [draftRows, setDraftRows] = useState<DisplayRow[]>([])
  const [savingDraftIds, setSavingDraftIds] = useState<Set<string>>(new Set())
  const [selectedSheetRow, setSelectedSheetRow] = useState<DisplayRow | null>(null)
  const [sheetSaving, setSheetSaving] = useState(false)
  const [sheetEditing, setSheetEditing] = useState(false)
  const [refreshAvailable, setRefreshAvailable] = useState(false)
  const [sheetFullscreen, setSheetFullscreen] = useState(false)
  const [sheetRevision, setSheetRevision] = useState(0)
  const [sheetFilterCriteria, setSheetFilterCriteria] = useState<
    Record<string, QuerySheetFilterCriteria>
  >({})
  const [pendingCount, setPendingCount] = useState(0)
  const [dataSourceMode, setDataSourceMode] = useState<'local' | 'tencent'>('local')
  const [addOpen, setAddOpen] = useState(false)
  const [addValues, setAddValues] = useState<Record<string, string>>({})
  const [adding, setAdding] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [drawerLoading, setDrawerLoading] = useState(false)
  const [drawerSources, setDrawerSources] = useState<QuerySourceRow[]>([])
  const [drawerSourceId, setDrawerSourceId] = useState<number>()
  const [drawerDraft, setDrawerDraft] = useState<Record<string, string>>({})
  const [drawerSaving, setDrawerSaving] = useState(false)
  const [auditOpen, setAuditOpen] = useState(false)
  const [auditRows, setAuditRows] = useState<QueryWritebackAudit[]>([])
  const [auditLoading, setAuditLoading] = useState(false)
  const [auditPage, setAuditPage] = useState(1)
  const [auditTotal, setAuditTotal] = useState(0)
  const fetchSequence = useRef(0)
  const dataVersionRef = useRef('')
  const versionContextRef = useRef('')
  const refreshBlockedRef = useRef(false)
  const pollingRef = useRef(false)
  const [messageApi, messageContext] = message.useMessage()

  const isSuperAdmin = user?.role === 'super_admin'
    || user?.permission_groups?.some(group => group.code === 'super_admin')
  const sheetRequestFilters = useMemo(
    () => buildQuerySheetRequestFilters(sheetFilterCriteria),
    [sheetFilterCriteria],
  )

  useEffect(() => {
    const handleFullscreenChange = () => {
      setSheetFullscreen(isQuerySheetFullscreen(
        document.fullscreenElement,
        document.documentElement,
      ))
    }
    document.addEventListener('fullscreenchange', handleFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange)
  }, [])

  useEffect(() => {
    document.documentElement.classList.toggle('query-sheet-fullscreen-active', sheetFullscreen)
    return () => document.documentElement.classList.remove('query-sheet-fullscreen-active')
  }, [sheetFullscreen])

  const handleSheetFullscreen = useCallback(async () => {
    try {
      await toggleQuerySheetFullscreen(
        document.documentElement,
        document.fullscreenElement,
        typeof document.exitFullscreen === 'function'
          ? () => document.exitFullscreen()
          : undefined,
      )
    } catch {
      messageApi.error('当前浏览器不支持全屏，请尝试更新浏览器或使用电脑端 Chrome')
    }
  }, [messageApi])

  useEffect(() => {
    getQueryTypes()
      .then(result => {
        setTypes(result)
        if (result.length && !result.includes(selectedType)) setSelectedType(result[0])
      })
      .catch(() => setError('业务类型加载失败'))
  }, [])

  const fetchData = useCallback(async (silent = false) => {
    const sequence = ++fetchSequence.current
    if (!silent) {
      setLoading(true)
      setError('')
    }
    try {
      const result = await loadCompleteQueryResult((requestPage, requestPageSize) => queryData({
        type: selectedType,
        source,
        page: requestPage,
        page_size: requestPageSize,
        keyword: keyword || undefined,
        filters: sheetRequestFilters.filters,
        grid_filters: sheetRequestFilters.gridFilters,
        sort_by: sortBy,
        sort_order: sortBy ? sortOrder : undefined,
      }))
      if (sequence !== fetchSequence.current) return
      setRows(result.data)
      setColumns(result.columns)
      setColumnMeta(result.column_meta || [])
      setDependentOptions(result.dependent_options)
      setTotal(result.total)
      setScopeMessage(result.scope_message || '')
      setRowManageMessage(result.row_manage_message || '')
      setSourceReady(Boolean(result.source_ready))
      setWritebackEnabled(true)
      setCanAdd(Boolean(result.can_add))
      setRequiredFields(result.required_fields || [])
      setPendingCount(0)
      setDataSourceMode('local')
      dataVersionRef.current = String(result.data_version || '')
      setRefreshAvailable(false)
      setMobilePage(current => Math.min(
        current,
        Math.max(1, Math.ceil(result.total / MOBILE_CARD_PAGE_SIZE)),
      ))
      setSelectedSheetRow(null)
      setSheetRevision(current => current + 1)
    } catch (requestError) {
      if (sequence !== fetchSequence.current) return
      if (silent) return
      setError(errorText(requestError, '查询失败，请检查网络后重试'))
      setRows([])
      setTotal(0)
      setSourceReady(false)
      setWritebackEnabled(false)
      setCanAdd(false)
      setDependentOptions(undefined)
      setRequiredFields([])
      setPendingCount(0)
      setDataSourceMode('local')
      setScopeMessage('')
      setRowManageMessage('')
      setSelectedSheetRow(null)
      setSheetRevision(current => current + 1)
    } finally {
      if (sequence === fetchSequence.current && !silent) setLoading(false)
    }
  }, [selectedType, source, keyword, sheetRequestFilters, sortBy, sortOrder])

  useEffect(() => {
    dataVersionRef.current = ''
    setRefreshAvailable(false)
  }, [selectedType, source])

  useEffect(() => { fetchData() }, [fetchData])

  const refreshBlocked = sheetSaving
    || sheetEditing
    || adding
    || drawerSaving
    || savingDraftIds.size > 0
    || draftRows.some(row => isQueryDraftTouched(row, columns))
  refreshBlockedRef.current = refreshBlocked

  const checkForUpdates = useCallback(async () => {
    if (source !== 'online' || pollingRef.current || document.visibilityState === 'hidden') return
    const requestContext = `${selectedType}:${source}`
    pollingRef.current = true
    try {
      const result = await getQueryDataVersion(selectedType)
      if (versionContextRef.current !== requestContext) return
      const latest = String(result.data_version || '')
      if (!dataVersionRef.current) {
        dataVersionRef.current = latest
      } else if (latest && latest !== dataVersionRef.current) {
        if (refreshBlockedRef.current) setRefreshAvailable(true)
        else await fetchData(true)
      }
    } catch {
      // Background freshness checks must not interrupt normal page use.
    } finally {
      pollingRef.current = false
    }
  }, [fetchData, selectedType, source])

  versionContextRef.current = `${selectedType}:${source}`

  useEffect(() => {
    if (source !== 'online') return
    const interval = window.setInterval(checkForUpdates, 15_000)
    const handleFocus = () => { void checkForUpdates() }
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') void checkForUpdates()
    }
    window.addEventListener('focus', handleFocus)
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', handleFocus)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [checkForUpdates, source])

  useEffect(() => {
    if (refreshAvailable && !refreshBlocked) void fetchData(true)
  }, [fetchData, refreshAvailable, refreshBlocked])

  const handleDelete = useCallback(async (row: DisplayRow) => {
    const sourceId = Number(row.__source_id)
    const revision = Number(row.__revision)
    if (!sourceId || !revision) return
    try {
      const result = await deleteQuerySourceRow(selectedType, sourceId, revision)
      messageApi.success(result.message)
      setDrawerOpen(false)
      await fetchData()
    } catch (requestError) {
      messageApi.error(errorText(requestError, '删除失败'))
      if (errorStatus(requestError) === 409) await fetchData()
    }
  }, [fetchData, messageApi, selectedType])

  const discardDraft = useCallback((draftId: string) => {
    setDraftRows(current => current.filter(row => row.__draft_id !== draftId))
    setSelectedSheetRow(null)
    setSheetRevision(current => current + 1)
  }, [])

  const submitDraft = useCallback(async (row: DisplayRow) => {
    const draftId = String(row.__draft_id || '')
    if (!draftId || savingDraftIds.has(draftId)) return
    const missing = missingQueryDraftFields(row, requiredFields)
    if (missing.length) {
      messageApi.warning(`请先填写：${missing.join('、')}`)
      return
    }
    setSavingDraftIds(current => new Set(current).add(draftId))
    try {
      const values = Object.fromEntries(
        columns.map(column => [column, String(row[column] ?? '')]),
      )
      const result = await createQuerySourceRow(selectedType, values)
      messageApi.success(result.message)
      setDraftRows(current => current.filter(item => item.__draft_id !== draftId))
      setSelectedSheetRow(null)
      await fetchData()
    } catch (requestError) {
      messageApi.error(errorText(requestError, '新增失败，草稿已保留'))
    } finally {
      setSavingDraftIds(current => {
        const next = new Set(current)
        next.delete(draftId)
        return next
      })
    }
  }, [
    columns,
    fetchData,
    messageApi,
    requiredFields,
    savingDraftIds,
    selectedType,
  ])

  const metaByColumn = useMemo(
    () => Object.fromEntries(columnMeta.map(meta => [meta.field, meta])),
    [columnMeta],
  )

  const handleSheetCommit = useCallback(async (changes: QuerySheetCellChange[]) => {
    const revisions = new Map<number, number>()
    const newlyPendingSourceIds = new Set<number>()
    let completed = 0
    try {
      for (const change of changes) {
        const sourceId = Number(change.row.__source_id)
        const initialRevision = Number(change.row.__revision)
        if (!sourceId || !initialRevision) throw new Error('缺少本地任务版本')
        const expectedRevision = revisions.get(sourceId) || initialRevision
        const result = await updateQuerySourceCell(selectedType, sourceId, {
          column: change.column,
          value: change.after,
          expected_revision: expectedRevision,
          explicit_text_edit: Boolean(change.explicitTextEdit),
        })
        revisions.set(sourceId, result.revision)
        const wasPending = Boolean(change.row.__pending)
        Object.assign(change.row, result.values, {
          __revision: result.revision,
          __row_key: result.row_key,
          __pending: result.pending_sync,
          __inspector_mismatch: Boolean(result.inspector_mismatch),
        })
        if (result.warnings?.length) {
          result.warnings.forEach(warning => messageApi.warning(warning))
        }
        if (result.pending_sync && !wasPending) {
          newlyPendingSourceIds.add(sourceId)
        }
        completed += 1
      }
      messageApi.success(changes.length > 1
        ? `已保存 ${changes.length} 个单元格到本地任务池`
        : '已保存到本地任务池')
      if (newlyPendingSourceIds.size > 0) {
        setPendingCount(current => current + newlyPendingSourceIds.size)
      }
    } catch (requestError) {
      const prefix = completed > 0 ? `已有 ${completed} 项写入；` : ''
      messageApi.error(`${prefix}${errorText(requestError, '保存失败，已重新加载在线内容')}`)
      await fetchData()
      throw requestError
    }
  }, [fetchData, messageApi, selectedType])

  const openAdd = () => {
    setAddValues(Object.fromEntries(columns.map(column => [column, ''])))
    setAddOpen(true)
  }

  const submitAdd = async () => {
    setAdding(true)
    try {
      const result = await createQuerySourceRow(selectedType, addValues)
      messageApi.success(result.message)
      setAddOpen(false)
      await fetchData()
    } catch (requestError) {
      messageApi.error(errorText(requestError, '新增失败'))
    } finally {
      setAdding(false)
    }
  }

  const openDetails = async (row: QueryDataRow) => {
    setDrawerOpen(true)
    setDrawerLoading(true)
    try {
      const rowKey = String(row.__row_key || '')
      let sources: QuerySourceRow[]
      if (rowKey && sourceReady) {
        sources = await getQuerySourceRows(selectedType, rowKey)
      } else {
        sources = []
      }
      setDrawerSources(sources)
      const first = sources[0]
      setDrawerSourceId(first?.id)
      setDrawerDraft(first ? { ...first.values } : Object.fromEntries(
        columns.map(column => [column, String(row[column] || '')]),
      ))
    } catch (requestError) {
      messageApi.error(errorText(requestError, '详情加载失败'))
    } finally {
      setDrawerLoading(false)
    }
  }

  const selectedDrawerSource = drawerSources.find(item => item.id === drawerSourceId)

  const saveDrawer = async () => {
    if (!selectedDrawerSource) return
    const changed = selectedDrawerSource.editable_fields.filter(
      column => drawerDraft[column] !== selectedDrawerSource.values[column],
    )
    if (!changed.length) {
      messageApi.info('没有需要保存的修改')
      return
    }
    setDrawerSaving(true)
    try {
      await saveChangedSourceFields(
        selectedDrawerSource,
        drawerDraft,
        (column, value, expectedRevision) => updateQuerySourceCell(
          selectedType,
          selectedDrawerSource.id,
          {
            column,
            value,
            expected_revision: expectedRevision,
            explicit_text_edit: true,
          },
        ),
      )
      messageApi.success('修改已保存到本地任务池')
      setDrawerOpen(false)
      await fetchData()
    } catch (requestError) {
      messageApi.error(errorText(requestError, '保存失败，请重新打开详情确认'))
      if (errorStatus(requestError) === 409) {
        setDrawerOpen(false)
        await fetchData()
      }
    } finally {
      setDrawerSaving(false)
    }
  }

  const loadAudit = useCallback(async (nextPage = 1) => {
    setAuditLoading(true)
    try {
      const result = await getQueryWritebackAudit({
        page: nextPage,
        page_size: 20,
        parser_type: selectedType,
      })
      setAuditRows(result.data)
      setAuditTotal(result.total)
      setAuditPage(nextPage)
    } catch (requestError) {
      messageApi.error(errorText(requestError, '修改记录加载失败'))
    } finally {
      setAuditLoading(false)
    }
  }, [messageApi, selectedType])

  const changeSourceType = (value: 'online' | 'archive') => {
    setSource(value)
    setMobilePage(1)
    setSheetFilterCriteria({})
    setSortBy(undefined)
    setSortOrder('asc')
    setRows([])
    setCanAdd(false)
    setRequiredFields([])
    setDraftRows([])
    setSelectedSheetRow(null)
  }

  const changeBusinessType = (value: string) => {
    setSelectedType(value)
    setMobilePage(1)
    setSheetFilterCriteria({})
    setSortBy(undefined)
    setSortOrder('asc')
    setRows([])
    setCanAdd(false)
    setRequiredFields([])
    setDraftRows([])
    setSelectedSheetRow(null)
  }

  return (
    <div className="app-page">
      {messageContext}
      <PageHeader
        title="在线数据查询"
        description="当前数据由本地任务池提供，按岗位和社区权限安全编辑；归档数据保持只读"
        actions={(
          <Space wrap>
            <Tag color="blue">共 {total} 条</Tag>
          </Space>
        )}
      />

      <section className="app-card query-filter-panel">
        <div className="app-toolbar query-filter-toolbar">
          <div className="contents md:hidden">
            <Select
              value={selectedType}
              onChange={changeBusinessType}
              className="min-w-44"
              options={types.map(type => ({ value: type, label: type }))}
            />
            <Segmented
              value={source}
              onChange={value => changeSourceType(value as 'online' | 'archive')}
              options={[
                { value: 'online', label: '当前数据' },
                { value: 'archive', label: '归档数据' },
              ]}
            />
          </div>
          <Input
            allowClear
            prefix={<SearchOutlined className="text-slate-400" />}
            placeholder="搜索全部字段"
            value={searchInput}
            onChange={event => setSearchInput(event.target.value)}
            onPressEnter={() => { setKeyword(searchInput); setMobilePage(1) }}
            className="min-w-56 flex-1"
          />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            onClick={() => { setKeyword(searchInput); setMobilePage(1) }}
          >
            搜索
          </Button>
          {source === 'online' && canAdd && (
            <Button className="md:hidden" icon={<PlusOutlined />} onClick={openAdd}>
                    新增业务数据
            </Button>
          )}
          {isSuperAdmin && (
            <Button
              icon={<HistoryOutlined />}
              onClick={() => { setAuditOpen(true); loadAudit(1) }}
            >
              修改记录
            </Button>
          )}
        </div>
      </section>

      {scopeMessage && <Alert type="info" showIcon message={scopeMessage} />}
      {source === 'online' && rowManageMessage && (
        <Alert type="warning" showIcon message={rowManageMessage} />
      )}
      {source === 'online' && refreshAvailable && (
        <Alert
          type="info"
          showIcon
          message="其他用户更新了当前数据"
          description="你正在编辑或有未保存草稿，系统暂不自动刷新。保存或清空后会自动载入最新数据。"
        />
      )}

      <div
        className={`app-card query-spreadsheet-card hidden overflow-hidden md:block${sheetFullscreen ? ' query-spreadsheet-card--fullscreen' : ''}`}
      >
        <div className="query-spreadsheet-toolbar">
          <div className="query-spreadsheet-toolbar__scope">
            <Select
              value={selectedType}
              onChange={changeBusinessType}
              className="query-spreadsheet-toolbar__type"
              options={types.map(type => ({ value: type, label: type }))}
            />
            <Segmented
              value={source}
              onChange={value => changeSourceType(value as 'online' | 'archive')}
              options={[
                { value: 'online', label: '当前数据' },
                { value: 'archive', label: '归档数据' },
              ]}
            />
          </div>
          <div className="query-spreadsheet-toolbar__status">
            {sortBy && (
              <>
                <Tag color="cyan">按 {sortBy} {sortOrder === 'asc' ? '升序' : '降序'}</Tag>
                <Button
                  size="small"
                  onClick={() => {
                    setSortBy(undefined)
                    setSortOrder('asc')
                    setMobilePage(1)
                    setSelectedSheetRow(null)
                  }}
                >
                  清除排序
                </Button>
              </>
            )}
            {Object.keys(sheetFilterCriteria).length > 0 && (
              <>
                <Tag color="blue">{Object.keys(sheetFilterCriteria).length} 列筛选中</Tag>
                <Button size="small" onClick={() => setSheetFilterCriteria({})}>清除筛选</Button>
              </>
            )}
            {!selectedSheetRow ? (
              <span className="text-sm text-[var(--app-text-secondary)]">
                选择单元格或整行后，可在这里查看来源状态和行操作
              </span>
            ) : selectedSheetRow.__kind === 'draft' ? (() => {
              const touched = isQueryDraftTouched(selectedSheetRow, columns)
              const draftId = String(selectedSheetRow.__draft_id || '')
              const missing = missingQueryDraftFields(selectedSheetRow, requiredFields)
              return touched ? (
                <>
                  <Tag color="gold">新增草稿</Tag>
                  {missing.length > 0 && <span className="text-sm text-amber-700">还需填写：{missing.join('、')}</span>}
                  <Button
                    type="primary"
                    size="small"
                    disabled={missing.length > 0}
                    loading={savingDraftIds.has(draftId)}
                    onClick={() => submitDraft(selectedSheetRow)}
                  >
                    写入本地任务池
                  </Button>
                  <Button size="small" onClick={() => discardDraft(draftId)}>清空这行</Button>
                </>
              ) : (
                <><Tag color="green" icon={<PlusOutlined />}>新增空行</Tag><span className="text-sm text-[var(--app-text-secondary)]">直接输入或粘贴内容即可开始新增</span></>
              )
            })() : (
              <>
                {Number(selectedSheetRow.__source_count || 0) > 1 ? (
                  <Tag>
                    {selectedSheetRow.__source_count} 条本地来源
                  </Tag>
                ) : selectedSheetRow.__editable_fields?.length ? (
                  <Tag color="blue">蓝色单元格可编辑</Tag>
                ) : <Tag>只读</Tag>}
                {selectedSheetRow.__pending && <Tag color="gold">汇总待同步</Tag>}
                {selectedSheetRow.__conflict && <Tag color="red">内容冲突</Tag>}
                <Button size="small" onClick={() => openDetails(selectedSheetRow)}>
                  {Number(selectedSheetRow.__source_count || 0) > 1 ? '查看原始行' : '查看详情'}
                </Button>
                {selectedSheetRow.__can_delete && (
                  <Popconfirm
                    title="确认移除本地任务？"
                    description="该操作会将当前业务数据归档，并从本地任务池移除。"
                    okText="确认删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    getPopupContainer={trigger => trigger.closest('.query-spreadsheet-card') as HTMLElement || document.body}
                    onConfirm={() => handleDelete(selectedSheetRow)}
                  >
                    <Button danger size="small" icon={<DeleteOutlined />}>归档任务</Button>
                  </Popconfirm>
                )}
              </>
            )}
          </div>
          <div className="query-spreadsheet-toolbar__actions">
            {sheetSaving && (
              <Tag color="processing">
                正在保存到滨湖平台
              </Tag>
            )}
            <Button
              size="small"
              icon={sheetFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
              aria-pressed={sheetFullscreen}
              onClick={handleSheetFullscreen}
            >
              {sheetFullscreen ? '退出全屏' : '全屏'}
            </Button>
          </div>
        </div>
        <Spin
          spinning={loading}
        tip="正在加载本地业务数据"
          className="query-spreadsheet-loading"
        >
          {columns.length > 0 ? (
            <QuerySpreadsheet
              businessType={selectedType}
              source={source}
              rows={rows}
              columns={columns}
              columnMeta={columnMeta}
              dependentOptions={dependentOptions}
              drafts={draftRows}
              canAdd={canAdd}
              revision={sheetRevision}
              layoutRevision={sheetFullscreen ? 1 : 0}
              filterCriteria={sheetFilterCriteria}
              onSortChange={(column, order) => {
                setSortBy(column)
                setSortOrder(order)
                setMobilePage(1)
                setSelectedSheetRow(null)
              }}
              onDraftsChange={setDraftRows}
              onFilterCriteriaChange={criteria => {
                setSheetFilterCriteria(criteria)
                setMobilePage(1)
              }}
              onSelectionChange={setSelectedSheetRow}
              onCommit={handleSheetCommit}
              onBlocked={messageApi.warning}
              onSavingChange={setSheetSaving}
              onEditingChange={setSheetEditing}
            />
          ) : (
            <div className="p-10"><Empty description={error || '没有找到符合条件的数据'} /></div>
          )}
        </Spin>
        <div className="border-t border-[var(--app-border)] bg-[var(--app-surface-muted)] px-4 py-2 text-xs text-[var(--app-text-secondary)]">
          蓝色单元格可直接编辑；工作表会连续加载全部查询结果。Univer 工具栏中的筛选和排序会重新查询全部记录，格式调整仅影响当前查看，不写入业务数据。
        </div>
      </div>

      <div className="data-query-mobile-list md:hidden">
        {loading ? <div className="app-card p-10 text-center"><Spin /></div> : rows.length === 0 ? (
          <div className="app-card p-8"><Empty description={error || '没有找到符合条件的数据'} /></div>
        ) : rows.slice(
          (mobilePage - 1) * MOBILE_CARD_PAGE_SIZE,
          mobilePage * MOBILE_CARD_PAGE_SIZE,
        ).map(row => {
          const community = String(row['社区'] || row['下发社区'] || '-')
          const name = String(row['姓名'] || row['参考姓名'] || row['出租屋地址'] || '-')
          const result = String(row['核查结果'] || row['核查反馈'] || row['实际情况'] || '尚未填写结果')
          return (
            <button
              type="button"
              key={String(row.__row_key)}
              className="app-card w-full p-4 text-left"
              onClick={() => openDetails(row)}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-base font-semibold text-[var(--app-text-strong)]">{name}</div>
                  <div className="mt-1 text-sm text-[var(--app-text-secondary)]">{community}</div>
                </div>
                <div className="flex flex-wrap justify-end gap-1">
                  {Number(row.__source_count || 0) > 1 && <Tag>{row.__source_count} 条原始行</Tag>}
                  {row.__pending && <Tag color="gold">待同步</Tag>}
                  {row.__conflict && <Tag color="red">冲突</Tag>}
                </div>
              </div>
              <div className="mt-3 line-clamp-2 text-sm text-[var(--app-text)]">{result}</div>
              <div className="mt-3 text-xs text-[var(--app-primary)]">查看详情与可编辑字段</div>
            </button>
          )
        })}
        {total > MOBILE_CARD_PAGE_SIZE && (
          <div className="flex justify-center py-2">
            <Pagination
              simple
              current={mobilePage}
              pageSize={MOBILE_CARD_PAGE_SIZE}
              total={total}
              onChange={setMobilePage}
            />
          </div>
        )}
      </div>

      <Modal
        open={addOpen}
        title={`新增“${selectedType}”业务数据`}
        width={840}
        okText="写入本地任务池"
        cancelText="取消"
        confirmLoading={adding}
        onOk={submitAdd}
        onCancel={() => setAddOpen(false)}
      >
        <Alert
          type="warning"
          showIcon
          message="新增会直接写入滨湖平台本地任务池"
          className="mb-4"
        />
        <div className="grid max-h-[60vh] grid-cols-1 gap-4 overflow-y-auto pr-2 md:grid-cols-2">
          {columns.map(column => {
            const meta = metaByColumn[column]
            const inspectorOptions = column === dependentOptions?.inspector_column
              ? queryInspectorOptions(dependentOptions, addValues)
              : null
            const options = inspectorOptions
              ? inspectorOptions.map(value => ({ value, label: value }))
              : (meta?.options || []).map(option => ({ value: option.text, label: option.text }))
            return (
              <label key={column} className="block">
                <span className="mb-1.5 block text-sm font-medium text-[var(--app-text)]">{column}</span>
                {meta?.type === 'select' ? (
                  <Select
                    className="w-full"
                    value={addValues[column] || undefined}
                    onChange={value => setAddValues(current => ({ ...current, [column]: value }))}
                    options={options}
                    allowClear
                    showSearch
                  />
                ) : (
                  <Input.TextArea
                    autoSize={{ minRows: 1, maxRows: 4 }}
                    value={addValues[column] || ''}
                    onChange={event => setAddValues(current => ({ ...current, [column]: event.target.value }))}
                  />
                )}
              </label>
            )
          })}
        </div>
        {queryInspectorMismatch(dependentOptions, addValues) && (
          <Alert
            className="mt-4"
            type="warning"
            showIcon
            message="核查人与社区不一致"
            description="原核查人已保留；重新选择时只能选择当前社区人员。"
          />
        )}
      </Modal>

      <Drawer
        open={drawerOpen}
        title="在线数据详情"
        width="min(94vw, 620px)"
        onClose={() => setDrawerOpen(false)}
        extra={selectedDrawerSource?.editable_fields.length ? (
          <Button type="primary" loading={drawerSaving} onClick={saveDrawer}>保存修改</Button>
        ) : null}
      >
        {drawerLoading ? <div className="py-16 text-center"><Spin /></div> : (
          <div className="query-drawer-content">
            {drawerSources.length > 1 && (
              <Select
                className="w-full"
                value={drawerSourceId}
                onChange={value => {
                  setDrawerSourceId(value)
                  const selected = drawerSources.find(item => item.id === value)
                  setDrawerDraft({ ...(selected?.values || {}) })
                }}
                options={drawerSources.map(item => ({
                  value: item.id,
                  label: `本地任务 #${item.id}`,
                }))}
              />
            )}
            {columns.map(column => {
              const editable = selectedDrawerSource?.editable_fields.includes(column)
              const managedMeta = metaByColumn[column]
              const sourceMeta = selectedDrawerSource?.cell_meta[column]
              const meta = managedMeta?.type === 'select'
                ? managedMeta
                : sourceMeta || managedMeta
              const inspectorOptions = column === dependentOptions?.inspector_column
                ? queryInspectorOptions(dependentOptions, drawerDraft)
                : null
              const options = inspectorOptions
                ? inspectorOptions.map(value => ({ value, label: value }))
                : (meta?.options || []).map(option => ({ value: option.text, label: option.text }))
              return (
                <label key={column} className="block">
                  <span className="mb-1 flex items-center justify-between text-sm font-medium text-[var(--app-text)]">
                    {column}
                    {editable && <Tag color="blue" icon={<EditOutlined />}>可编辑</Tag>}
                  </span>
                  {editable && meta?.type === 'select' ? (
                    <Select
                      className="w-full"
                      value={drawerDraft[column] || undefined}
                      onChange={value => setDrawerDraft(current => ({ ...current, [column]: value || '' }))}
                      options={options}
                      allowClear
                      showSearch
                    />
                  ) : editable ? (
                    <Input.TextArea
                      autoSize={{ minRows: 1, maxRows: 5 }}
                      value={drawerDraft[column] || ''}
                      onChange={event => setDrawerDraft(current => ({ ...current, [column]: event.target.value }))}
                    />
                  ) : (
                    <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-surface-muted)] px-3 py-2 text-sm text-[var(--app-text)]">
                      {drawerDraft[column] || '-'}
                    </div>
                  )}
                </label>
              )
            })}
            {queryInspectorMismatch(dependentOptions, drawerDraft) && (
              <Alert
                type="warning"
                showIcon
                message="核查人与社区不一致"
                description="原值不会自动清空；重新选择核查人时只能选择当前社区在岗人员。"
              />
            )}
            {selectedDrawerSource?.can_delete && (
              <Popconfirm
                title="确认移除本地任务？"
                description="删除后会立即从本地任务池移除并进入归档。"
                okText="确认删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => handleDelete(sourceToDisplay(selectedDrawerSource, 'drawer'))}
              >
                <Button danger block icon={<DeleteOutlined />}>
                  移除这条本地任务
                </Button>
              </Popconfirm>
            )}
          </div>
        )}
      </Drawer>

      <Modal
        open={auditOpen}
        title={`“${selectedType}”本地修改记录（保留 90 天）`}
        width={980}
        footer={null}
        onCancel={() => setAuditOpen(false)}
      >
        <Spin spinning={auditLoading}>
          <List
            dataSource={auditRows}
            locale={{ emptyText: '暂无修改记录' }}
            renderItem={item => {
              const changes = buildQueryAuditChanges(
                item.before_values,
                item.after_values,
                item.action,
              )
              return (
                <List.Item className="!block">
                  <div className="w-full">
                    <div className="flex flex-wrap items-center gap-2">
                      <Tag color={item.action === 'delete' ? 'red' : item.action === 'create' ? 'green' : 'blue'}>
                        {{ create: '新增', update: '修改', delete: '删除' }[item.action]}
                      </Tag>
                      <span className="font-medium text-[var(--app-text-strong)]">{item.username}</span>
                      <span className="text-sm text-[var(--app-text-secondary)]">
                        {item.column_name || `第 ${item.physical_row || '-'} 行`}
                      </span>
                      <Tag color={item.sync_status === 'synced' ? 'green' : item.sync_status === 'failed' ? 'red' : 'gold'}>
                        {item.sync_status === 'synced' ? '已同步' : item.sync_status === 'failed' ? '写入失败' : '待同步'}
                      </Tag>
                    </div>
                    <div className="mt-1 text-xs text-[var(--app-text-tertiary)]">
                      {formatUTCTime(item.created_at, systemTimezone)} · {changes.length} 个字段发生变化
                    </div>
                    <div className="mt-3 overflow-hidden rounded-lg border border-[var(--app-border)]">
                      {changes.length ? changes.map(change => (
                        <div
                          key={change.field}
                          className="grid gap-1 border-b border-[var(--app-border)] px-3 py-2 text-sm last:border-b-0 md:grid-cols-[140px_1fr]"
                        >
                          <div className="font-medium text-[var(--app-text)]">{change.field}</div>
                          {item.action === 'create' ? (
                            <div className="break-words text-emerald-600 dark:text-emerald-400">+ {change.after}</div>
                          ) : item.action === 'delete' ? (
                            <div className="break-words text-red-600 dark:text-red-400">− {change.before}</div>
                          ) : (
                            <div className="flex flex-wrap items-center gap-2 break-words">
                              <span className="rounded bg-red-50 px-1.5 py-0.5 text-red-700 line-through dark:bg-red-950/40 dark:text-red-300">
                                {change.before || '（空）'}
                              </span>
                              <span className="text-[var(--app-text-tertiary)]">→</span>
                              <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                                {change.after || '（空）'}
                              </span>
                            </div>
                          )}
                        </div>
                      )) : (
                        <div className="px-3 py-2 text-sm text-[var(--app-text-secondary)]">没有可展示的字段变化</div>
                      )}
                    </div>
                  </div>
                </List.Item>
              )
            }}
          />
          {auditTotal > 20 && (
            <div className="mt-4 flex justify-end">
              <Pagination current={auditPage} pageSize={20} total={auditTotal} onChange={loadAudit} />
            </div>
          )}
        </Spin>
      </Modal>
    </div>
  )
}
