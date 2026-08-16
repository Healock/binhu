import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, Input, Modal, Select, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import { CheckCircleOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, StopOutlined } from '@ant-design/icons'
import {
  createCommunityArea,
  deleteCommunityArea,
  getAreaLeaderOptions,
  getCommunityAreas,
  getGridCommunities,
  addGridCommunity,
  deleteGridCommunity,
  getCommunityPoliceOptions,
  updateGridCommunityDetails,
  updateCommunityArea,
  updateGridCommunityStatus,
  type CommunityArea,
  type GridCommunity,
} from '../api/client'
import AppTable from '../components/AppTable'
import { EmptyState, ListToolbar, LoadingState, PageHeader } from '../components/ui'
import { useAuth } from '../context/AuthContext'

export default function Communities() {
  const { user } = useAuth()
  const canManage = Boolean(user?.permissions.includes('community.manage'))
  const [communities, setCommunities] = useState<GridCommunity[]>([])
  const [areas, setAreas] = useState<CommunityArea[]>([])
  const [areaLeaderOptions, setAreaLeaderOptions] = useState<Array<{ id: number; name: string }>>([])
  const [newName, setNewName] = useState('')
  const [msg, setMsg] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [editingCommunity, setEditingCommunity] = useState<GridCommunity | null>(null)
  const [nameDraft, setNameDraft] = useState('')
  const [aliasDraft, setAliasDraft] = useState<string[]>([])
  const [officerDraft, setOfficerDraft] = useState<number[]>([])
  const [areaDraft, setAreaDraft] = useState<number>()
  const [qmfCommunityCodeDraft, setQmfCommunityCodeDraft] = useState('')
  const [policeOptions, setPoliceOptions] = useState<Array<{ id: number; name: string }>>([])
  const [savingDetails, setSavingDetails] = useState(false)
  const [areaEditorOpen, setAreaEditorOpen] = useState(false)
  const [editingArea, setEditingArea] = useState<CommunityArea | null>(null)
  const [areaNameDraft, setAreaNameDraft] = useState('')
  const [areaLeaderDraft, setAreaLeaderDraft] = useState<number[]>([])
  const [savingArea, setSavingArea] = useState(false)
  const [keyword, setKeyword] = useState('')

  const fetch = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const [communityRows, officerRows, areaRows, leaderRows] = await Promise.all([
        getGridCommunities(),
        getCommunityPoliceOptions(),
        getCommunityAreas(),
        getAreaLeaderOptions(),
      ])
      setCommunities(communityRows)
      setPoliceOptions(officerRows)
      setAreas(areaRows)
      setAreaLeaderOptions(leaderRows)
    } catch {
      setLoadError('社区列表加载失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetch() }, [fetch])

  const handleAdd = async () => {
    if (!newName.trim()) return
    try { await addGridCommunity(newName.trim()); setNewName(''); setMsg('添加成功'); fetch() }
    catch (e: any) { setMsg(e?.response?.data?.detail || '添加失败') }
  }

  const handleDelete = (id: number, name: string) => {
    Modal.confirm({
      title: '删除社区',
      content: `确认删除社区“${name}”？`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteGridCommunity(id)
          setMsg(`已删除社区“${name}”`)
          fetch()
        } catch (error: any) {
          setMsg(`删除失败：${error?.response?.data?.detail || '请稍后重试'}`)
        }
      },
    })
  }

  const openCommunityEditor = (community: GridCommunity) => {
    setEditingCommunity(community)
    setNameDraft(community.name)
    setAliasDraft(community.aliases || [])
    setOfficerDraft(community.police_officer_ids || [])
    setAreaDraft(community.area_id || undefined)
    setQmfCommunityCodeDraft(community.qmf_community_code || '')
  }

  const handleSaveDetails = async () => {
    if (!editingCommunity) return
    if (!areaDraft) {
      setMsg('保存失败：请选择所属片区')
      return
    }
    setSavingDetails(true)
    try {
      const result = await updateGridCommunityDetails(
        editingCommunity.id,
        nameDraft.trim(),
        aliasDraft,
        officerDraft,
        areaDraft,
        qmfCommunityCodeDraft.trim(),
      )
      const matchedText = result.matched_visit_rows > 0
        ? `，同时归类 ${result.matched_visit_rows} 条已有走访数据`
        : ''
      setMsg(`“${editingCommunity.name}”的社区资料已保存${matchedText}`)
      setEditingCommunity(null)
      setNameDraft('')
      setAliasDraft([])
      setOfficerDraft([])
      setAreaDraft(undefined)
      setQmfCommunityCodeDraft('')
      await fetch()
    } catch (error: any) {
      setMsg(`保存失败：${error?.response?.data?.detail || '请稍后重试'}`)
    } finally {
      setSavingDetails(false)
    }
  }

  const openAreaEditor = (area?: CommunityArea) => {
    setEditingArea(area || null)
    setAreaNameDraft(area?.name || '')
    setAreaLeaderDraft(area?.leader_ids || [])
    setAreaEditorOpen(true)
  }

  const saveArea = async () => {
    if (!areaNameDraft.trim()) return
    setSavingArea(true)
    try {
      const payload = { name: areaNameDraft.trim(), leader_ids: areaLeaderDraft }
      if (editingArea) await updateCommunityArea(editingArea.id, payload)
      else await createCommunityArea(payload)
      setMsg(editingArea ? '片区已保存' : '片区已创建')
      setAreaEditorOpen(false)
      await fetch()
    } catch (error: any) {
      setMsg(`保存失败：${error?.response?.data?.detail || '请稍后重试'}`)
    } finally {
      setSavingArea(false)
    }
  }

  const removeArea = (area: CommunityArea) => {
    Modal.confirm({
      title: `删除片区“${area.name}”？`,
      content: '只有没有关联社区的片区才能删除。',
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteCommunityArea(area.id)
          setMsg(`已删除片区“${area.name}”`)
          await fetch()
        } catch (error: any) {
          setMsg(`删除失败：${error?.response?.data?.detail || '请稍后重试'}`)
        }
      },
    })
  }

  const communityColumns: TableColumnsType<GridCommunity> = [
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 90,
      render: value => <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag>,
    },
    {
      title: '所属片区',
      dataIndex: 'area_name',
      key: 'area_name',
      width: 140,
      render: value => value ? <Tag color="blue">{value}</Tag> : <Tag color="warning">待分配</Tag>,
    },
    {
      title: '全民防社区代码',
      dataIndex: 'qmf_community_code',
      key: 'qmf_community_code',
      width: 170,
      render: value => value || <Tag color="warning">待填写</Tag>,
    },
    {
      title: '社区名称',
      dataIndex: 'name',
      key: 'name',
      width: 240,
      sorter: (left, right) => left.name.localeCompare(right.name, 'zh-CN'),
      render: value => <span className="font-medium text-slate-800">{value}</span>,
    },
    {
      title: '人员数量',
      dataIndex: 'grid_count',
      key: 'grid_count',
      width: 160,
      sorter: (left, right) => left.grid_count - right.grid_count,
    },
    {
      title: '社区民警',
      dataIndex: 'police_officers',
      key: 'police_officers',
      width: 260,
      render: officers => officers?.length > 0
        ? <span>{officers.join('、')}</span>
        : <span className="text-slate-400">暂未填写</span>,
    },
    {
      title: '别名',
      dataIndex: 'aliases',
      key: 'aliases',
      width: 300,
      render: aliases => aliases?.length > 0
        ? (
          <div className="flex flex-wrap gap-1">
            {aliases.map(alias => <Tag key={alias}>{alias}</Tag>)}
          </div>
        )
        : <span className="text-slate-400">暂无别名</span>,
    },
    ...(canManage ? [{
      title: '操作',
      key: 'actions',
      width: 300,
      render: (_, community) => (
        <div className="flex items-center gap-1">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openCommunityEditor(community)}
          >
            编辑资料
          </Button>
          <Button
            type="link"
            size="small"
            danger={community.is_active}
            icon={community.is_active ? <StopOutlined /> : <CheckCircleOutlined />}
            onClick={() => {
              Modal.confirm({
                title: community.is_active ? `停用“${community.name}”？` : `重新启用“${community.name}”？`,
                content: community.is_active
                  ? '停用社区不会删除历史数据；存在人员或未完成下发任务时将拒绝停用。'
                  : '启用后可重新参与人员分配、地址匹配和下发任务平均分配。',
                okText: community.is_active ? '停用' : '启用',
                okButtonProps: { danger: community.is_active },
                cancelText: '取消',
                onOk: async () => {
                  try {
                    const result = await updateGridCommunityStatus(community.id, !community.is_active)
                    setMsg(result.message)
                    await fetch()
                  } catch (error: any) {
                    const detail = error?.response?.data?.detail
                    setMsg(`状态修改失败：${typeof detail === 'string' ? detail : detail?.message || '请稍后重试'}`)
                  }
                },
              })
            }}
          >
            {community.is_active ? '停用' : '启用'}
          </Button>
          <Button type="link" danger size="small" onClick={() => handleDelete(community.id, community.name)}>
            删除
          </Button>
        </div>
      ),
    }] : []),
  ]
  const normalizedKeyword = keyword.trim().toLocaleLowerCase()
  const visibleCommunities = normalizedKeyword
    ? communities.filter(community => [
      community.name,
      community.area_name,
      community.qmf_community_code,
      ...(community.aliases || []),
      ...(community.police_officers || []),
    ].filter(Boolean).join(' ').toLocaleLowerCase().includes(normalizedKeyword))
    : communities

  return (
    <div className="app-page">
      <PageHeader
        title="社区管理"
        description="维护片区、社区名单、别名和社区民警；片长的在线编辑范围以片区配置为准"
      />

      <section className="app-card app-card--padded">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-[var(--app-text-strong)]">片区设置</h2>
            <p className="mt-1 text-sm text-[var(--app-text-secondary)]">一个社区只能属于一个片区；同一片区可以配置多位片长。</p>
          </div>
          {canManage && <Button icon={<PlusOutlined />} onClick={() => openAreaEditor()}>添加片区</Button>}
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          {areas.map(area => (
            <div key={area.id} className="rounded-lg border border-[var(--app-border)] bg-[var(--app-surface-muted)] p-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-semibold text-[var(--app-text-strong)]">{area.name}</div>
                  <div className="mt-1 text-sm text-[var(--app-text-secondary)]">{area.community_count} 个社区</div>
                </div>
                {canManage && <div className="flex gap-1">
                  <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openAreaEditor(area)} />
                  <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={() => removeArea(area)} />
                </div>}
              </div>
              <div className="mt-3 flex flex-wrap gap-1">
                {area.leaders.length
                  ? area.leaders.map(leader => <Tag key={leader.id}>{leader.name}</Tag>)
                  : <span className="text-xs text-[var(--app-text-muted)]">暂未配置片长</span>}
              </div>
            </div>
          ))}
        </div>
      </section>

      {canManage && <section className="app-card">
        <div className="app-toolbar">
          <Input
            value={newName}
            onChange={event => setNewName(event.target.value)}
            onPressEnter={handleAdd}
            placeholder="输入社区名称"
            className="min-w-56 flex-1"
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd} disabled={!newName.trim()}>
            添加社区
          </Button>
        </div>
        {msg && <Alert type={msg.includes('失败') ? 'error' : 'success'} showIcon message={msg} />}
      </section>}

      <ListToolbar
        filters={<Input allowClear prefix={<SearchOutlined />} value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索社区、片区、别名或社区民警" className="w-full md:w-80" />}
        meta={<Tag color="blue">当前 {visibleCommunities.length} 个社区</Tag>}
        actions={<Button icon={<ReloadOutlined />} onClick={() => void fetch()}>刷新</Button>}
      />

      {loading ? (
        <div className="app-table-wrap">
          <LoadingState />
        </div>
      ) : loadError ? (
        <div className="app-table-wrap">
          <EmptyState label={loadError} />
        </div>
      ) : visibleCommunities.length === 0 ? (
        <div className="app-table-wrap">
          <EmptyState label={communities.length ? '没有符合条件的社区' : '暂无社区，可在上方输入社区名称后添加'} />
        </div>
      ) : (
        <>
          <div className="app-table-wrap md:hidden">
            <div className="grid grid-cols-1 gap-3 p-4">
              {visibleCommunities.map((c) => (
                <div key={c.id} className="border border-gray-200 rounded-lg p-4 flex items-center justify-between">
                  <div>
                    <div className="font-medium text-gray-800">{c.name}</div>
                    <Tag color={c.is_active ? 'success' : 'default'}>{c.is_active ? '启用' : '停用'}</Tag>
                    <div className="text-sm text-gray-500">人员 {c.grid_count} 人</div>
                    <div className="mt-1 text-sm text-slate-600">片区：{c.area_name || '待分配'}</div>
                    <div className="mt-1 text-sm text-slate-600">全民防社区代码：{c.qmf_community_code || '待填写'}</div>
                    <div className="mt-1 text-sm text-slate-600">
                      社区民警：{c.police_officers?.length > 0
                        ? c.police_officers.join('、')
                        : '暂未填写'}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1">
                      {c.aliases?.length > 0
                        ? c.aliases.map(alias => <Tag key={alias}>{alias}</Tag>)
                        : <span className="text-xs text-slate-400">暂无别名</span>}
                    </div>
                  </div>
                  {canManage && <div className="flex flex-col items-end">
                    <Button type="link" size="small" onClick={() => openCommunityEditor(c)}>编辑资料</Button>
                    <Button type="link" danger size="small" onClick={() => handleDelete(c.id, c.name)}>删除</Button>
                  </div>}
                </div>
              ))}
            </div>
          </div>
          <div className="hidden md:block">
            <AppTable<GridCommunity>
              columns={communityColumns}
              dataSource={visibleCommunities}
              rowKey="id"
              scroll={{ x: 900 }}
            />
          </div>
        </>
      )}
      <p className="text-xs text-slate-500">人员数量会根据“人员管理”中的社区部门自动统计，无需手动填写。</p>

      {canManage && <Modal
        open={Boolean(editingCommunity)}
        title={editingCommunity ? `编辑“${editingCommunity.name}”` : '编辑社区资料'}
        okText="保存"
        cancelText="取消"
        confirmLoading={savingDetails}
        onOk={handleSaveDetails}
        onCancel={() => {
          setEditingCommunity(null)
          setNameDraft('')
          setAliasDraft([])
          setOfficerDraft([])
          setAreaDraft(undefined)
          setQmfCommunityCodeDraft('')
        }}
      >
        <div className="space-y-5">
          <div>
            <div className="mb-2 font-medium text-slate-700">所属片区</div>
            <Select
              value={areaDraft}
              onChange={setAreaDraft}
              placeholder="请选择所属片区"
              className="w-full"
              options={areas.map(area => ({ value: area.id, label: area.name }))}
            />
          </div>
          <div>
            <div className="mb-2 font-medium text-slate-700">全民防社区代码</div>
            <Input
              value={qmfCommunityCodeDraft}
              onChange={event => setQmfCommunityCodeDraft(event.target.value.replace(/\D/g, '').slice(0, 10))}
              placeholder="请输入全民防中的 10 位社区代码"
              inputMode="numeric"
              maxLength={10}
            />
            <p className="mt-2 text-sm text-slate-500">
              “离开不返吴”反馈会使用该代码；未填写时系统会在写入前停止。
            </p>
          </div>
          <div>
            <div className="mb-2 font-medium text-slate-700">社区正式名称</div>
            <Input
              value={nameDraft}
              onChange={event => setNameDraft(event.target.value)}
              placeholder="请输入社区正式名称"
            />
            <p className="mt-2 text-sm text-slate-500">
              修改后所属部门会同步改名，旧名称会自动保留为别名。
            </p>
          </div>
          <div>
            <div className="mb-2 font-medium text-slate-700">社区民警</div>
            <p className="mb-3 text-sm text-slate-500">
              从人员管理中已登记的社区民警里选择。人员可以同时负责多个社区，工作日志中会用“、”连接。
            </p>
            <Select
              mode="multiple"
              value={officerDraft}
              onChange={setOfficerDraft}
              placeholder="请选择社区民警"
              className="w-full"
              maxTagCount="responsive"
              options={policeOptions.map(item => ({
                value: item.id,
                label: item.name,
              }))}
            />
          </div>
          <div>
            <div className="mb-2 font-medium text-slate-700">社区别名</div>
            <p className="mb-3 text-sm text-slate-500">
              按来源数据中的完整名称填写，按回车添加。例如正式名称为“南厍”时，可以添加别名“南厍村”。系统不会自动删除“社区”或“村”。
            </p>
            <Select
              mode="tags"
              value={aliasDraft}
              onChange={setAliasDraft}
              tokenSeparators={[',', '，']}
              placeholder="例如：芦荡"
              className="w-full"
              maxTagCount="responsive"
              options={[]}
            />
          </div>
        </div>
      </Modal>}

      {canManage && <Modal
        open={areaEditorOpen}
        title={editingArea ? `编辑“${editingArea.name}”` : '添加片区'}
        okText="保存"
        cancelText="取消"
        confirmLoading={savingArea}
        onOk={saveArea}
        onCancel={() => setAreaEditorOpen(false)}
      >
        <div className="space-y-4">
          <div>
            <div className="mb-2 font-medium text-slate-700">片区名称</div>
            <Input value={areaNameDraft} onChange={event => setAreaNameDraft(event.target.value)} placeholder="例如：东片" />
          </div>
          <div>
            <div className="mb-2 font-medium text-slate-700">片长</div>
            <Select
              mode="multiple"
              value={areaLeaderDraft}
              onChange={setAreaLeaderDraft}
              className="w-full"
              placeholder="可选择一名或多名片长"
              options={areaLeaderOptions.map(item => ({ value: item.id, label: item.name }))}
              maxTagCount="responsive"
            />
          </div>
        </div>
      </Modal>}
    </div>
  )
}
