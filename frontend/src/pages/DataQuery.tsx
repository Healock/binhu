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
  DownOutlined,
  EditOutlined,
  HistoryOutlined,
  PlusOutlined,
  RightOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import {
  AllCommunityModule,
  ModuleRegistry,
  themeQuartz,
  type CellValueChangedEvent,
  type ColDef,
  type FilterChangedEvent,
  type GridApi,
  type SortChangedEvent,
} from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import {
  createQuerySourceRow,
  deleteQuerySourceRow,
  getQuerySourceRows,
  getQueryTypes,
  getQueryWritebackAudit,
  queryData,
  updateQuerySourceCell,
  type QueryColumnMeta,
  type QueryDataRow,
  type QuerySourceRow,
  type QueryWritebackAudit,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { PageHeader } from '../components/ui'
import {
  buildQueryDisplayRows,
  buildQueryAuditChanges,
  canEditQueryCell,
  ensureTrailingQueryDraft,
  isQueryDraftTouched,
  missingQueryDraftFields,
  normalizeQueryResponse,
  saveChangedSourceFields,
  sourceToDisplay,
  type QueryDisplayRow as DisplayRow,
} from '../utils/queryGrid'

ModuleRegistry.registerModules([AllCommunityModule])

const gridTheme = themeQuartz.withParams({
  accentColor: '#2563eb',
  backgroundColor: 'var(--app-surface)',
  foregroundColor: 'var(--app-text)',
  borderColor: 'var(--app-border)',
  headerBackgroundColor: 'var(--app-surface-muted)',
  oddRowBackgroundColor: 'var(--app-surface-muted)',
  rowHoverColor: 'var(--app-primary-soft)',
  fontFamily: 'inherit',
  fontSize: 13,
  spacing: 6,
})

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
  const { user } = useAuth()
  const [types, setTypes] = useState<string[]>([])
  const [selectedType, setSelectedType] = useState('全链条')
  const [source, setSource] = useState<'online' | 'archive'>('online')
  const [searchInput, setSearchInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [rows, setRows] = useState<QueryDataRow[]>([])
  const [columns, setColumns] = useState<string[]>([])
  const [columnMeta, setColumnMeta] = useState<QueryColumnMeta[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [sortBy, setSortBy] = useState<string>()
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc')
  const [gridFilters, setGridFilters] = useState<Record<string, unknown>>({})
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
  const [pendingCount, setPendingCount] = useState(0)
  const [expanded, setExpanded] = useState<Record<string, QuerySourceRow[]>>({})
  const [expanding, setExpanding] = useState<string>()
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
  const gridApi = useRef<GridApi<DisplayRow> | null>(null)
  const fetchSequence = useRef(0)
  const nextDraftId = useRef(0)
  const [messageApi, messageContext] = message.useMessage()

  const makeDraftId = useCallback(
    () => `new-${++nextDraftId.current}`,
    [],
  )

  const isSuperAdmin = user?.role === 'super_admin'
    || user?.permission_groups?.some(group => group.code === 'super_admin')

  useEffect(() => {
    getQueryTypes()
      .then(result => {
        setTypes(result)
        if (result.length && !result.includes(selectedType)) setSelectedType(result[0])
      })
      .catch(() => setError('业务类型加载失败'))
  }, [])

  const fetchData = useCallback(async () => {
    const sequence = ++fetchSequence.current
    setLoading(true)
    setError('')
    try {
      const result = normalizeQueryResponse(await queryData({
        type: selectedType,
        source,
        page,
        page_size: pageSize,
        keyword: keyword || undefined,
        sort_by: sortBy,
        sort_order: sortOrder,
        grid_filters: Object.keys(gridFilters).length ? gridFilters : undefined,
      }))
      if (sequence !== fetchSequence.current) return
      setRows(result.data)
      setColumns(result.columns)
      setColumnMeta(result.column_meta || [])
      setTotal(result.total)
      setScopeMessage(result.scope_message || '')
      setRowManageMessage(result.row_manage_message || '')
      setSourceReady(Boolean(result.source_ready))
      setWritebackEnabled(Boolean(result.writeback_enabled))
      setCanAdd(Boolean(result.can_add))
      setRequiredFields(result.required_fields || [])
      setPendingCount(Number(result.pending_count || 0))
      setExpanded({})
    } catch (requestError) {
      if (sequence !== fetchSequence.current) return
      setError(errorText(requestError, '查询失败，请检查网络后重试'))
      setRows([])
      setTotal(0)
      setSourceReady(false)
      setWritebackEnabled(false)
      setCanAdd(false)
      setRequiredFields([])
      setPendingCount(0)
      setScopeMessage('')
      setRowManageMessage('')
    } finally {
      if (sequence === fetchSequence.current) setLoading(false)
    }
  }, [selectedType, source, page, pageSize, keyword, sortBy, sortOrder, gridFilters])

  useEffect(() => { fetchData() }, [fetchData])

  const columnsKey = columns.join('\u0001')
  useEffect(() => {
    setDraftRows(current => {
      if (source !== 'online' || !canAdd || columns.length === 0) return []
      return ensureTrailingQueryDraft(current, columns, makeDraftId)
    })
  }, [canAdd, columnsKey, makeDraftId, source])

  const displayRows = useMemo<DisplayRow[]>(() => {
    return buildQueryDisplayRows(rows, expanded)
  }, [rows, expanded])

  const toggleSources = useCallback(async (row: QueryDataRow) => {
    const rowKey = String(row.__row_key || '')
    if (!rowKey) return
    if (expanded[rowKey]) {
      setExpanded(current => {
        const next = { ...current }
        delete next[rowKey]
        return next
      })
      return
    }
    setExpanding(rowKey)
    try {
      const sources = await getQuerySourceRows(selectedType, rowKey)
      setExpanded(current => ({ ...current, [rowKey]: sources }))
    } catch (requestError) {
      messageApi.error(errorText(requestError, '原始行加载失败'))
    } finally {
      setExpanding(undefined)
    }
  }, [expanded, messageApi, selectedType])

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
    setDraftRows(current => ensureTrailingQueryDraft(
      current.filter(row => row.__draft_id !== draftId),
      columns,
      makeDraftId,
    ))
  }, [columns, makeDraftId])

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
      setDraftRows(current => ensureTrailingQueryDraft(
        current.filter(item => item.__draft_id !== draftId),
        columns,
        makeDraftId,
      ))
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
    makeDraftId,
    messageApi,
    requiredFields,
    savingDraftIds,
    selectedType,
  ])

  const actionRenderer = useCallback((params: { data?: DisplayRow }) => {
    const row = params.data
    if (!row) return null
    if (row.__kind === 'draft') {
      const draftId = String(row.__draft_id || '')
      const touched = isQueryDraftTouched(row, columns)
      const missing = missingQueryDraftFields(row, requiredFields)
      return (
        <div className="flex h-full items-center gap-1.5">
          {!touched ? (
            <Tag color="green" icon={<PlusOutlined />}>在此新增</Tag>
          ) : (
            <>
              <Tag color="gold">待提交</Tag>
              <Button
                type="link"
                size="small"
                disabled={missing.length > 0}
                loading={savingDraftIds.has(draftId)}
                title={missing.length ? `还需填写：${missing.join('、')}` : '写入腾讯表格'}
                onClick={() => submitDraft(row)}
              >
                写入
              </Button>
              <Button
                type="text"
                danger
                size="small"
                icon={<DeleteOutlined />}
                title="放弃这条草稿"
                onClick={() => discardDraft(draftId)}
              />
            </>
          )}
        </div>
      )
    }
    const duplicate = row.__kind === 'parent' && Number(row.__source_count || 0) > 1
    const key = String(row.__row_key || '')
    return (
      <div className={`flex h-full items-center gap-1 ${row.__kind === 'source' ? 'pl-4' : ''}`}>
        {duplicate && (
          <Button
            type="text"
            size="small"
            loading={expanding === key}
            icon={expanded[key] ? <DownOutlined /> : <RightOutlined />}
            onClick={() => toggleSources(row)}
          >
            {row.__source_count} 条原始行
          </Button>
        )}
        {row.__kind === 'source' && <Tag color="default">第 {String(row.__physical_row)} 行</Tag>}
        {row.__pending && <Tag color="gold">待同步</Tag>}
        {row.__conflict && <Tag color="red">内容冲突</Tag>}
        {row.__can_delete && (
          <Popconfirm
            title="确认删除腾讯原始行？"
            description="该操作会真实删除在线表格中的整行，下一次同步后进入归档。"
            okText="确认删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => handleDelete(row)}
          >
            <Button type="text" danger size="small" icon={<DeleteOutlined />} />
          </Popconfirm>
        )}
      </div>
    )
  }, [
    columns,
    discardDraft,
    expanded,
    expanding,
    handleDelete,
    requiredFields,
    savingDraftIds,
    submitDraft,
    toggleSources,
  ])

  const metaByColumn = useMemo(
    () => Object.fromEntries(columnMeta.map(meta => [meta.field, meta])),
    [columnMeta],
  )

  const columnDefs = useMemo<ColDef<DisplayRow>[]>(() => [
    {
      headerName: '来源 / 状态',
      colId: '__actions',
      width: 210,
      minWidth: 180,
      pinned: 'left',
      sortable: false,
      filter: false,
      editable: false,
      cellRenderer: actionRenderer,
    },
    ...columns.map((column, index) => {
      const meta = metaByColumn[column] || { type: 'text' }
      const definition: ColDef<DisplayRow> = {
        field: column,
        headerName: requiredFields.includes(column) ? `${column} *` : column,
        minWidth: 150,
        width: 180,
        pinned: index === 0 ? 'left' : undefined,
        filter: meta.type === 'number' ? 'agNumberColumnFilter' : 'agTextColumnFilter',
        filterParams: { debounceMs: 450, buttons: ['reset'] },
        sortable: true,
        resizable: true,
        editable: params => canEditQueryCell(source, params.data, column, canAdd),
        cellClassRules: {
          'binhu-grid-cell--editable': params => canEditQueryCell(
            source,
            params.data,
            column,
            canAdd,
          ),
          'binhu-grid-cell--draft-required': params => Boolean(
            params.data?.__kind === 'draft'
            && requiredFields.includes(column)
            && !String(params.value ?? '').trim()
          ),
        },
        tooltipValueGetter: params => String(params.value || ''),
      }
      if (meta.type === 'select') {
        definition.cellEditor = 'agSelectCellEditor'
        definition.cellEditorParams = {
          values: (meta.options || []).map(option => option.text),
        }
      } else if (meta.type === 'number') {
        definition.cellEditor = 'agNumberCellEditor'
      } else if (['地址', '现住址', '核查结果', '核查反馈', '二次反馈', '二次核查结果', '实际情况'].includes(column)) {
        definition.flex = 1
        definition.minWidth = 220
      }
      return definition
    }),
  ], [actionRenderer, canAdd, columns, metaByColumn, requiredFields, source])

  const handleCellChange = useCallback(async (event: CellValueChangedEvent<DisplayRow>) => {
    const row = event.data
    const column = event.colDef.field
    if (!row || !column || event.newValue === event.oldValue) return
    if (row.__kind === 'draft') {
      const draftId = String(row.__draft_id || '')
      setDraftRows(current => ensureTrailingQueryDraft(
        current.map(item => item.__draft_id === draftId
          ? { ...item, [column]: String(event.newValue ?? '') }
          : item),
        columns,
        makeDraftId,
      ))
      return
    }
    const sourceId = Number(row.__source_id)
    const revision = Number(row.__revision)
    if (!sourceId || !revision) return
    try {
      const result = await updateQuerySourceCell(selectedType, sourceId, {
        column,
        value: String(event.newValue ?? ''),
        expected_revision: revision,
      })
      messageApi.success('已写回腾讯表格')
      await fetchData()
    } catch (requestError) {
      row[column] = event.oldValue
      event.api.refreshCells({ rowNodes: [event.node], columns: [column], force: true })
      messageApi.error(errorText(requestError, '保存失败，已恢复原值'))
      if (errorStatus(requestError) === 409) await fetchData()
    }
  }, [columns, fetchData, makeDraftId, messageApi, selectedType])

  const handleSort = useCallback((event: SortChangedEvent<DisplayRow>) => {
    const sorted = event.api.getColumnState().find(column => column.sort)
    setSortBy(sorted?.colId === '__actions' ? undefined : sorted?.colId)
    setSortOrder(sorted?.sort === 'asc' ? 'asc' : 'desc')
    setPage(1)
  }, [])

  const handleFilter = useCallback((event: FilterChangedEvent<DisplayRow>) => {
    setGridFilters(event.api.getFilterModel())
    setPage(1)
  }, [])

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

  const openMobileDetails = async (row: QueryDataRow) => {
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
          { column, value, expected_revision: expectedRevision },
        ),
      )
      messageApi.success('修改已写回腾讯表格')
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
    setPage(1)
    setGridFilters({})
    setRows([])
    setCanAdd(false)
    setRequiredFields([])
    setDraftRows([])
    setExpanded({})
    gridApi.current?.setFilterModel(null)
  }

  return (
    <div className="app-page">
      {messageContext}
      <PageHeader
        title="在线数据查询"
        description="当前数据可按岗位和社区权限安全回写腾讯表格；归档数据保持只读"
        actions={(
          <Space wrap>
            {pendingCount > 0 && <Tag color="gold">{pendingCount} 项待同步</Tag>}
            <Tag color="blue">共 {total} 条</Tag>
          </Space>
        )}
      />

      <section className="app-card app-card--padded">
        <div className="app-toolbar">
          <Select
            value={selectedType}
            onChange={value => {
              setSelectedType(value)
              setPage(1)
              setRows([])
              setCanAdd(false)
              setRequiredFields([])
              setDraftRows([])
              setExpanded({})
            }}
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
          <Input
            allowClear
            prefix={<SearchOutlined className="text-slate-400" />}
            placeholder="搜索全部字段"
            value={searchInput}
            onChange={event => setSearchInput(event.target.value)}
            onPressEnter={() => { setKeyword(searchInput); setPage(1) }}
            className="min-w-56 flex-1"
          />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            onClick={() => { setKeyword(searchInput); setPage(1) }}
          >
            搜索
          </Button>
          {source === 'online' && canAdd && (
            <Button className="md:hidden" icon={<PlusOutlined />} onClick={openAdd}>
              新增原始行
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
      {source === 'online' && sourceReady && pendingCount > 0 && (
        <Alert
          type="warning"
          showIcon
          message="腾讯表格已更新，业务库和汇总尚未同步"
          description="查询页显示最新来源内容；下一次正常同步后，在线汇总、归档和日报才会更新。"
        />
      )}
      {source === 'online' && sourceReady && !writebackEnabled && (
        <Alert type="warning" showIcon message="超级管理员已暂停在线回写，当前页面只读" />
      )}

      <div className="app-card hidden overflow-hidden md:block">
        <div style={{ height: 620 }}>
          <AgGridReact<DisplayRow>
            theme={gridTheme}
            rowData={displayRows}
            pinnedBottomRowData={source === 'online' && canAdd ? draftRows : undefined}
            columnDefs={columnDefs}
            defaultColDef={{ suppressHeaderMenuButton: false }}
            getRowId={params => params.data.__kind === 'draft'
              ? `draft-${params.data.__draft_id}`
              : params.data.__kind === 'source'
                ? `source-${params.data.__source_id}`
                : `parent-${params.data.__row_key}`}
            getRowClass={params => params.data?.__kind === 'draft'
              ? 'binhu-grid-row--draft'
              : params.data?.__kind === 'source'
                ? 'binhu-grid-row--source'
                : params.data?.__conflict ? 'binhu-grid-row--conflict' : undefined}
            loading={loading}
            tooltipShowDelay={300}
            stopEditingWhenCellsLoseFocus
            singleClickEdit
            animateRows={false}
            onGridReady={event => { gridApi.current = event.api }}
            onCellValueChanged={handleCellChange}
            onSortChanged={handleSort}
            onFilterChanged={handleFilter}
            postSortRows={params => {
              const children = new Map<string, typeof params.nodes>()
              for (const node of params.nodes) {
                const parentKey = String(node.data?.__parent_key || '')
                if (node.data?.__kind === 'source' && parentKey) {
                  const group = children.get(parentKey) || []
                  group.push(node)
                  children.set(parentKey, group)
                }
              }
              const ordered = [] as typeof params.nodes
              const included = new Set(params.nodes)
              for (const node of params.nodes) {
                if (node.data?.__kind === 'source') continue
                ordered.push(node)
                const key = String(node.data?.__row_key || '')
                for (const child of children.get(key) || []) {
                  ordered.push(child)
                  included.delete(child)
                }
              }
              for (const node of included) {
                if (node.data?.__kind === 'source') ordered.push(node)
              }
              params.nodes.splice(0, params.nodes.length, ...ordered)
            }}
            overlayNoRowsTemplate={error || '没有找到符合条件的数据'}
          />
        </div>
        {source === 'online' && canAdd && (
          <div className="border-t border-[var(--app-border)] bg-[var(--app-surface-muted)] px-4 py-2 text-xs text-[var(--app-text-secondary)]">
            在表格最下方的新增空行中直接填写；开始录入后会自动补一条空行。草稿只保留在当前页面，点击“写入”后才会提交到腾讯表格。
          </div>
        )}
        <div className="flex justify-end border-t border-[var(--app-border)] px-4 py-3">
          <Pagination
            current={page}
            pageSize={pageSize}
            total={total}
            showSizeChanger
            pageSizeOptions={[20, 50, 100, 200]}
            showTotal={count => `共 ${count} 条`}
            onChange={(nextPage, nextSize) => {
              setPage(nextSize !== pageSize ? 1 : nextPage)
              setPageSize(nextSize)
            }}
          />
        </div>
      </div>

      <div className="space-y-3 md:hidden">
        {loading ? <div className="app-card p-10 text-center"><Spin /></div> : rows.length === 0 ? (
          <div className="app-card p-8"><Empty description={error || '没有找到符合条件的数据'} /></div>
        ) : rows.map(row => {
          const community = String(row['社区'] || row['下发社区'] || '-')
          const name = String(row['姓名'] || row['参考姓名'] || row['出租屋地址'] || '-')
          const result = String(row['核查结果'] || row['核查反馈'] || row['实际情况'] || '尚未填写结果')
          return (
            <button
              type="button"
              key={String(row.__row_key)}
              className="app-card w-full p-4 text-left"
              onClick={() => openMobileDetails(row)}
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
        {total > pageSize && (
          <div className="flex justify-center py-2">
            <Pagination simple current={page} pageSize={pageSize} total={total} onChange={setPage} />
          </div>
        )}
      </div>

      <Modal
        open={addOpen}
        title={`新增“${selectedType}”腾讯原始行`}
        width={840}
        okText="写入腾讯表格"
        cancelText="取消"
        confirmLoading={adding}
        onOk={submitAdd}
        onCancel={() => setAddOpen(false)}
      >
        <Alert
          type="warning"
          showIcon
          message="新增会直接写入腾讯在线表格，业务汇总需等待下一次正常同步"
          className="mb-4"
        />
        <div className="grid max-h-[60vh] grid-cols-1 gap-4 overflow-y-auto pr-2 md:grid-cols-2">
          {columns.map(column => {
            const meta = metaByColumn[column]
            return (
              <label key={column} className="block">
                <span className="mb-1.5 block text-sm font-medium text-[var(--app-text)]">{column}</span>
                {meta?.type === 'select' ? (
                  <Select
                    className="w-full"
                    value={addValues[column] || undefined}
                    onChange={value => setAddValues(current => ({ ...current, [column]: value }))}
                    options={(meta.options || []).map(option => ({ value: option.text, label: option.text }))}
                    allowClear
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
          <div className="space-y-4">
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
                  label: `腾讯第 ${item.physical_row} 行`,
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
                      options={(meta.options || []).map(option => ({ value: option.text, label: option.text }))}
                      allowClear
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
            {selectedDrawerSource?.can_delete && (
              <Popconfirm
                title="确认删除腾讯原始行？"
                description="删除后需等待下一次同步更新业务汇总。"
                okText="确认删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => handleDelete(sourceToDisplay(selectedDrawerSource, 'drawer'))}
              >
                <Button danger block icon={<DeleteOutlined />}>删除这条腾讯原始行</Button>
              </Popconfirm>
            )}
          </div>
        )}
      </Drawer>

      <Modal
        open={auditOpen}
        title={`“${selectedType}”平台回写记录（保留 90 天）`}
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
                      {new Date(item.created_at).toLocaleString('zh-CN')} · {changes.length} 个字段发生变化
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
