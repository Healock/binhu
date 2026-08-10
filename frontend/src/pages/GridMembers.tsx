import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Collapse,
  DatePicker,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Pagination,
  Radio,
  Select,
  Skeleton,
  Tag,
  Tooltip,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  CalendarOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  HistoryOutlined,
  InfoCircleOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { useNavigate } from 'react-router-dom'
import AppTable from '../components/AppTable'
import {
  createGridMember,
  deleteGridMember,
  exportGridMembersUrl,
  getAttendanceScheduleStatus,
  getGridCommunities,
  getDepartments,
  getAttendanceHistory,
  getMemberAccountOptions,
  getUnlinkedAccountOptions,
  listGridMembers,
  updateGridMember,
  updateGridMemberLeave,
  type GridCommunity,
  type GridMember,
  type AttendanceHistoryItem,
  type AttendanceScheduleStatus,
  type DepartmentOption,
  type AccountOption,
} from '../api/client'
import {
  PERSONNEL_POSITIONS,
  type PersonnelPosition,
} from '../constants/personnel'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'

export default function GridMembers() {
  const { user } = useAuth()
  const navigate = useNavigate()
  type PersonnelCategory = 'flow_work' | 'internal_business' | 'police_leadership'
  type CategoryState = {
    rows: GridMember[]
    total: number
    page: number
    loading: boolean
    error: string
  }
  const categories: Array<{
    key: PersonnelCategory
    label: string
    description: string
  }> = [
    { key: 'flow_work', label: '流口工作', description: '组员、组长、自购房、片长' },
    { key: 'internal_business', label: '内勤业务', description: '基础管控、中队长' },
    { key: 'police_leadership', label: '民警与领导', description: '社区民警、所队领导' },
  ]
  const emptyCategoryState = (): CategoryState => ({
    rows: [], total: 0, page: 1, loading: false, error: '',
  })
  const [categoryStates, setCategoryStates] = useState<
    Record<PersonnelCategory, CategoryState>
  >({
    flow_work: emptyCategoryState(),
    internal_business: emptyCategoryState(),
    police_leadership: emptyCategoryState(),
  })
  const [communities, setCommunities] = useState<GridCommunity[]>([])
  const [departments, setDepartments] = useState<DepartmentOption[]>([])
  const [accountOptions, setAccountOptions] = useState<AccountOption[]>([])
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [communityFilter, setCommunityFilter] = useState('')
  const [positionFilter, setPositionFilter] = useState('')
  const [editing, setEditing] = useState<GridMember | null>(null)
  const [leaveEditing, setLeaveEditing] = useState<GridMember | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyRows, setHistoryRows] = useState<AttendanceHistoryItem[]>([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [showAddForm, setShowAddForm] = useState(false)
  const [msg, setMsg] = useState('')
  const [scheduleRange, setScheduleRange] = useState<[string, string]>(() => [
    dayjs().startOf('month').format('YYYY-MM-DD'),
    dayjs().endOf('month').format('YYYY-MM-DD'),
  ])
  const [scheduleStatus, setScheduleStatus] = useState<AttendanceScheduleStatus | null>(null)
  const [scheduleLoading, setScheduleLoading] = useState(false)
  const [scheduleError, setScheduleError] = useState('')
  const pageSize = 20
  const canManage = Boolean(user?.permissions.includes('personnel.manage'))
  const canManageAttendance = Boolean(user?.permissions.includes('attendance.manage'))
  const canViewSensitive = Boolean(user?.permissions.includes('personnel.sensitive.view'))
  const canManageIdentity = user?.role === 'super_admin'
  const canDelete = Boolean(
    user?.permission_groups.some(group => group.code === 'super_admin'),
  )

  const fetch = useCallback(async () => {
    setCategoryStates(previous => Object.fromEntries(
      Object.entries(previous).map(([key, value]) => [
        key,
        { ...value, loading: true, error: '' },
      ]),
    ) as Record<PersonnelCategory, CategoryState>)
    await Promise.all(categories.map(async ({ key }) => {
      try {
        const response = await listGridMembers({
          keyword: keyword || undefined,
          community: communityFilter || undefined,
          position: positionFilter || undefined,
          category: key,
          page: categoryStates[key].page,
          page_size: pageSize,
        })
        setCategoryStates(previous => ({
          ...previous,
          [key]: {
            ...previous[key],
            rows: response.data,
            total: response.total,
            loading: false,
            error: '',
          },
        }))
      } catch {
        setCategoryStates(previous => ({
          ...previous,
          [key]: {
            ...previous[key],
            rows: [],
            total: 0,
            loading: false,
            error: '人员列表加载失败，请稍后重试',
          },
        }))
      }
    }))
  }, [keyword, communityFilter, positionFilter, categoryStates.flow_work.page,
    categoryStates.internal_business.page, categoryStates.police_leadership.page])

  const fetchCommunities = useCallback(async () => {
    try {
      const [communityRows, departmentRows, accountRows] = await Promise.all([
        getGridCommunities(),
        getDepartments(),
        canManage ? getUnlinkedAccountOptions() : Promise.resolve([]),
      ])
      setCommunities(communityRows)
      setDepartments(departmentRows)
      setAccountOptions(accountRows)
    } catch {
      // 人员列表仍然可以独立显示。
    }
  }, [canManage])

  useEffect(() => { fetch() }, [fetch])
  useEffect(() => { fetchCommunities() }, [fetchCommunities])
  const checkSchedule = useCallback(async (
    range: [string, string] = scheduleRange,
  ) => {
    setScheduleLoading(true)
    setScheduleError('')
    try {
      setScheduleStatus(await getAttendanceScheduleStatus(range[0], range[1]))
    } catch (error: any) {
      setScheduleStatus(null)
      setScheduleError(
        error?.response?.data?.detail || '排班状态读取失败，请稍后重试',
      )
    } finally {
      setScheduleLoading(false)
    }
  }, [scheduleRange])
  useEffect(() => {
    if (canManageAttendance) void checkSchedule()
  }, [canManageAttendance, checkSchedule])
  useEffect(() => {
    if (!historyOpen || !canViewSensitive) return
    setHistoryLoading(true)
    getAttendanceHistory({ page: 1, page_size: 200 })
      .then(response => {
        setHistoryRows(response.data)
        setHistoryTotal(response.total)
      })
      .catch(() => {
        setHistoryRows([])
        setHistoryTotal(0)
      })
      .finally(() => setHistoryLoading(false))
  }, [canViewSensitive, historyOpen])

  const refresh = () => {
    void fetch()
    void fetchCommunities()
  }

  const handleDelete = (id: number, name: string) => {
    Modal.confirm({
      title: '删除人员',
      content: `确认联动删除人员“${name}”及其登录账号？相关会话会立即失效，此操作只应清理误建资料。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteGridMember(id)
          setMsg(`已删除人员“${name}”`)
          refresh()
        } catch {
          setMsg('删除失败，请稍后重试')
        }
      },
    })
  }

  const communityNames = communities.map(community => community.name)
  const total = categories.reduce(
    (sum, category) => sum + categoryStates[category.key].total,
    0,
  )
  const memberColumns: TableColumnsType<GridMember> = [
    {
      title: '姓名',
      dataIndex: 'name',
      key: 'name',
      width: 110,
      render: (_, member) => (
        <MemberProfileLink
          member={member}
          onOpen={() => navigate(`/people/${member.account?.id}`, { state: { returnTo: '/grid-members', returnLabel: '返回人员管理' } })}
        />
      ),
    },
    {
      title: '所属部门',
      key: 'department',
      width: 150,
      render: (_, member) => `${member.departments?.map(item => item.name).join('、') || member.department?.name || '未分配部门'} · ${member.position}`,
    },
    {
      title: '岗位',
      dataIndex: 'position',
      key: 'position',
      width: 110,
      render: value => <Tag color="blue">{value || '组员'}</Tag>,
    },
    {
      title: '电话',
      dataIndex: 'phone',
      key: 'phone',
      width: 140,
      render: value => value || '-',
    },
    {
      title: '身份证号',
      dataIndex: 'id_card_number',
      key: 'id_card_number',
      width: 185,
      render: value => value || <span className="text-slate-400">未补齐</span>,
    },
    {
      title: '人员状态',
      key: 'status',
      width: 210,
      render: (_, member) => <MemberStatus member={member} />,
    },
    {
      title: '备注',
      dataIndex: 'notes',
      key: 'notes',
      width: 200,
      ellipsis: { showTitle: false },
      render: value => (
        <Tooltip title={value || '-'}>
          <span>{value || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      fixed: 'right',
      render: (_, member) => (
        <div className="whitespace-nowrap">
          {canManage && <Button type="link" size="small" onClick={() => setEditing(member)}>
            编辑
          </Button>}
          {canManageAttendance && <Button type="link" size="small" onClick={() => setLeaveEditing(member)}>
            请假
          </Button>}
          {canDelete && <Button
            type="link"
            danger
            size="small"
            onClick={() => handleDelete(member.id, member.name)}
          >
            删除
          </Button>}
        </div>
      ),
    },
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="人员管理"
        description="查看人员所属部门、岗位和在岗状态；有权限的账号可以维护资料与出勤"
        actions={(
          <>
            {canViewSensitive && <Button
              icon={<HistoryOutlined />}
              onClick={() => setHistoryOpen(true)}
            >
              出勤记录
            </Button>}
            {canManageAttendance && <Button
              icon={<CalendarOutlined />}
              onClick={() => navigate('/grid-members/weekend-duty')}
            >
              双休日备勤
            </Button>}
            {canViewSensitive && <Button
              icon={<DownloadOutlined />}
              onClick={() => window.open(exportGridMembersUrl(), '_blank')}
            >
              导出 CSV
            </Button>}
            {canManage && <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setShowAddForm(true)}
            >
              添加人员
            </Button>}
          </>
        )}
      />

      {canManageAttendance && <Panel
        title="双休日排班检查"
        description="只检查所选区间内截至今天已经发生、但仍未完成的双休日排班"
      >
        <div className="flex flex-col gap-3">
          <div className="flex min-w-0 flex-col gap-2 md:flex-row md:items-center">
            <div className="flex w-full items-center gap-1.5 md:hidden">
              <input
                type="date"
                value={scheduleRange[0]}
                onChange={(event) => {
                  const start = event.target.value
                  if (!start) return
                  setScheduleRange([
                    start,
                    start > scheduleRange[1] ? start : scheduleRange[1],
                  ])
                }}
                className="min-h-11 min-w-0 flex-1 rounded border border-gray-300 px-2 text-sm"
              />
              <span className="text-xs text-gray-400">至</span>
              <input
                type="date"
                value={scheduleRange[1]}
                onChange={(event) => {
                  const end = event.target.value
                  if (!end) return
                  setScheduleRange([
                    end < scheduleRange[0] ? end : scheduleRange[0],
                    end,
                  ])
                }}
                className="min-h-11 min-w-0 flex-1 rounded border border-gray-300 px-2 text-sm"
              />
            </div>
            <DatePicker.RangePicker
              className="hidden w-[300px] md:flex"
              value={[dayjs(scheduleRange[0]), dayjs(scheduleRange[1])]}
              allowClear={false}
              onChange={(_, dateStrings) => {
                if (dateStrings[0] && dateStrings[1]) {
                  setScheduleRange([dateStrings[0], dateStrings[1]])
                }
              }}
            />
            <Button
              icon={<CalendarOutlined />}
              loading={scheduleLoading}
              onClick={() => checkSchedule()}
            >
              检查排班
            </Button>
          </div>

          {scheduleError && (
            <Alert type="error" showIcon message={scheduleError} />
          )}
          {scheduleStatus && !scheduleStatus.complete && (
            <Alert
              type="warning"
              showIcon
              message="截至今天仍有双休日未完成排班"
              description={`涉及周次：${scheduleStatus.missing_week_starts.join('、')}。这些历史周次完成排班前，对应日期的走访人均日数据不会显示；未来周次不会提前提示。`}
              action={(
                <Button
                  size="small"
                  onClick={() => navigate(
                    `/grid-members/weekend-duty?week=${scheduleStatus.missing_week_starts[0]}`,
                  )}
                >
                  去安排
                </Button>
              )}
            />
          )}
          {scheduleStatus?.complete && (
            <Alert
              type="success"
              showIcon
              message="所选区间的双休日排班已完整"
            />
          )}
        </div>
      </Panel>}

      <section className="app-card">
        <div className="app-toolbar">
          <Input
            allowClear
            prefix={<SearchOutlined className="text-slate-400" />}
            placeholder="搜索姓名、电话或岗位"
            value={searchInput}
            onChange={event => setSearchInput(event.target.value)}
            onPressEnter={() => {
              setKeyword(searchInput)
              setCategoryStates(previous => ({
                ...previous,
                flow_work: { ...previous.flow_work, page: 1 },
                internal_business: { ...previous.internal_business, page: 1 },
                police_leadership: { ...previous.police_leadership, page: 1 },
              }))
            }}
            className="w-full md:min-w-56 md:flex-1"
          />
          <Select
            value={communityFilter}
            onChange={value => {
              setCommunityFilter(value)
              setCategoryStates(previous => ({
                ...previous,
                flow_work: { ...previous.flow_work, page: 1 },
                internal_business: { ...previous.internal_business, page: 1 },
                police_leadership: { ...previous.police_leadership, page: 1 },
              }))
            }}
            className="w-[calc(50%-6px)] md:w-auto md:min-w-36"
            options={[
              { value: '', label: '全部社区部门' },
              ...communityNames.map(community => ({
                value: community,
                label: community,
              })),
            ]}
          />
          <Select
            value={positionFilter}
            onChange={value => {
              setPositionFilter(value)
              setCategoryStates(previous => ({
                ...previous,
                flow_work: { ...previous.flow_work, page: 1 },
                internal_business: { ...previous.internal_business, page: 1 },
                police_leadership: { ...previous.police_leadership, page: 1 },
              }))
            }}
            className="w-[calc(50%-6px)] md:w-auto md:min-w-36"
            options={[
              { value: '', label: '全部岗位' },
              ...PERSONNEL_POSITIONS.map(position => ({
                value: position,
                label: position,
              })),
            ]}
          />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            className="w-full md:w-auto"
            onClick={() => {
              setKeyword(searchInput)
              setCategoryStates(previous => ({
                ...previous,
                flow_work: { ...previous.flow_work, page: 1 },
                internal_business: { ...previous.internal_business, page: 1 },
                police_leadership: { ...previous.police_leadership, page: 1 },
              }))
            }}
          >
            搜索
          </Button>
          <div className="flex w-full flex-wrap gap-2 md:ml-auto md:w-auto">
            <Tag color="blue">共 {total} 人</Tag>
          </div>
        </div>
        {msg && (
          <Alert
            type={msg.includes('失败') ? 'error' : 'success'}
            showIcon
            message={msg}
          />
        )}
      </section>

      <Collapse
        defaultActiveKey={categories.map(category => category.key)}
        className="personnel-category-list"
        items={categories.map(category => {
          const state = categoryStates[category.key]
          const changePage = (page: number) => setCategoryStates(previous => ({
            ...previous,
            [category.key]: { ...previous[category.key], page },
          }))
          return {
            key: category.key,
            label: (
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="font-semibold text-slate-800">{category.label}</span>
                <Tag>{state.total} 人</Tag>
                <span className="text-xs text-slate-400">{category.description}</span>
              </div>
            ),
            children: (
              <>
                <div className="hidden md:block">
                  <AppTable<GridMember>
                    columns={memberColumns.filter(column => (
                      (canViewSensitive || column.key !== 'notes')
                      && (canManageIdentity || column.key !== 'id_card_number')
                    ))}
                    dataSource={state.rows}
                    emptyText={state.error || '该分类暂无人员'}
                    loading={state.loading}
                    pagination={{
                      current: state.page,
                      pageSize,
                      total: state.total,
                      hideOnSinglePage: true,
                      showSizeChanger: false,
                      showTotal: count => `共 ${count} 人`,
                      onChange: changePage,
                    }}
                    rowClassName={member => (
                      ['离岗', '休息'].includes(member.effective_status)
                        ? 'app-table-row--muted'
                        : ''
                    )}
                    rowKey="id"
                    scroll={{ x: 1250 }}
                  />
                </div>
                <div className="md:hidden">
                  {state.loading ? (
                    <Card size="small"><Skeleton active paragraph={{ rows: 4 }} /></Card>
                  ) : state.rows.length ? (
                    <div className="space-y-3">
                      {state.rows.map(member => (
                        <MobileMemberCard
                          key={member.id}
                          member={member}
                          onViewProfile={() => navigate(`/people/${member.account?.id}`, { state: { returnTo: '/grid-members', returnLabel: '返回人员管理' } })}
                          onEdit={() => setEditing(member)}
                          onLeave={() => setLeaveEditing(member)}
                          onDelete={() => handleDelete(member.id, member.name)}
                          canManage={canManage}
                          canDelete={canDelete}
                          canManageAttendance={canManageAttendance}
                          canViewSensitive={canViewSensitive}
                          canManageIdentity={canManageIdentity}
                        />
                      ))}
                    </div>
                  ) : (
                    <Card size="small">
                      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={state.error || '该分类暂无人员'} />
                    </Card>
                  )}
                  {!state.loading && state.total > pageSize && (
                    <div className="mt-4 flex justify-center">
                      <Pagination
                        simple
                        current={state.page}
                        pageSize={pageSize}
                        total={state.total}
                        showSizeChanger={false}
                        onChange={changePage}
                      />
                    </div>
                  )}
                </div>
              </>
            ),
          }
        })}
      />

      {canManage && (showAddForm || editing) && (
        <MemberForm
          member={editing}
          departments={departments}
          accountOptions={accountOptions}
          canManageIdentity={canManageIdentity}
          onClose={() => {
            setShowAddForm(false)
            setEditing(null)
          }}
          onSaved={() => {
            setShowAddForm(false)
            setEditing(null)
            setMsg(editing ? '人员信息已更新' : '人员已添加')
            refresh()
          }}
        />
      )}

      {leaveEditing && (
        <LeaveModal
          member={leaveEditing}
          onClose={() => setLeaveEditing(null)}
          onSaved={message => {
            setLeaveEditing(null)
            setMsg(message)
            refresh()
          }}
        />
      )}

      <Drawer
        open={historyOpen}
        width={720}
        title={`出勤记录（共 ${historyTotal} 条）`}
        onClose={() => setHistoryOpen(false)}
      >
        <Alert
          showIcon
          type="info"
          message="这里保留临时请假和长期离岗历史；双休日在岗安排请到“双休日备勤”查看"
          className="mb-4"
        />
        <List
          loading={historyLoading}
          dataSource={historyRows}
          locale={{ emptyText: '暂无出勤变动记录' }}
          renderItem={item => (
            <List.Item>
              <div className="w-full min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-slate-800">
                    {item.member_name}
                  </span>
                  <Tag color={item.absence_type === 'long_term_leave' ? 'default' : 'orange'}>
                    {item.absence_type === 'long_term_leave' ? '长期离岗' : '临时请假'}
                  </Tag>
                  {!item.is_active && <Tag>已撤销</Tag>}
                </div>
                <div className="mt-1 text-sm text-slate-600">
                  {item.start_date}
                  {' 至 '}
                  {item.end_date || '长期'}
                </div>
                {item.reason && (
                  <div className="mt-1 text-sm text-slate-500">
                    原因：{item.reason}
                  </div>
                )}
              </div>
            </List.Item>
          )}
        />
        {historyTotal > historyRows.length && (
          <p className="mt-3 text-center text-xs text-slate-500">
            当前显示最近 {historyRows.length} 条记录
          </p>
        )}
      </Drawer>
    </div>
  )
}

function getMemberStatusMeta(member: GridMember) {
  const isLongTerm = member.status === '离岗'
  const isActiveLeave = !isLongTerm && member.effective_status === '离岗'
  const isWeekendRest = member.effective_status === '休息'
  const isWeekendUnscheduled = member.effective_status === '未排班'
  const isUpcoming = !isLongTerm && member.leave_state === 'upcoming'
  const label = isLongTerm
    ? '长期离岗'
    : isActiveLeave
    ? '请假中'
    : isWeekendRest
    ? '休息'
    : isWeekendUnscheduled
    ? '未排班'
    : isUpcoming
    ? '待请假'
    : '正常'
  const color = isLongTerm
    ? 'default'
    : isActiveLeave
    ? 'orange'
    : isWeekendRest
    ? 'cyan'
    : isWeekendUnscheduled
    ? 'gold'
    : isUpcoming
    ? 'blue'
    : 'green'
  const detail = isLongTerm ? '' : (member.status_detail || '').trim()
  const reason = (
    isLongTerm || isActiveLeave || isUpcoming
  ) ? (member.leave_reason || '').trim() : ''

  return { label, color, detail, reason }
}

function MemberStatus({ member }: { member: GridMember }) {
  const { label, color, detail, reason } = getMemberStatusMeta(member)
  const dotClassName = color === 'green'
    ? 'bg-green-500'
    : color === 'orange'
    ? 'bg-orange-500'
    : color === 'cyan'
    ? 'bg-cyan-500'
    : color === 'gold'
    ? 'bg-amber-500'
    : color === 'blue'
    ? 'bg-blue-500'
    : 'bg-slate-400'

  return (
    <div className="min-w-[180px] py-0.5">
      <div className="flex min-w-0 items-center gap-2">
        <span
          aria-hidden="true"
          className={`h-2 w-2 shrink-0 rounded-full ${dotClassName}`}
        />
        <span className="shrink-0 text-sm font-medium text-slate-700">
          {label}
        </span>
        {detail && (
          <span className="min-w-0 truncate text-xs text-slate-500" title={detail}>
            {detail}
          </span>
        )}
      </div>
      {reason && (
        <div
          className="mt-1 truncate pl-4 text-xs text-slate-500"
          title={`原因：${reason}`}
        >
          原因：{reason}
        </div>
      )}
    </div>
  )
}

function MemberProfileLink({
  member,
  onOpen,
}: {
  member: GridMember
  onOpen: () => void
}) {
  if (!member.account?.id) {
    return <span className="font-medium text-[var(--app-text)]">{member.name}</span>
  }
  return (
    <button
      type="button"
      className="personnel-profile-link"
      aria-label={`查看${member.name}的个人资料`}
      onClick={onOpen}
    >
      {member.name}
    </button>
  )
}

function MobileMemberCard({
  member,
  onViewProfile,
  onEdit,
  onLeave,
  onDelete,
  canManage,
  canDelete,
  canManageAttendance,
  canViewSensitive,
  canManageIdentity,
}: {
  member: GridMember
  onViewProfile: () => void
  onEdit: () => void
  onLeave: () => void
  onDelete: () => void
  canManage: boolean
  canDelete: boolean
  canManageAttendance: boolean
  canViewSensitive: boolean
  canManageIdentity: boolean
}) {
  const { label, color, detail, reason } = getMemberStatusMeta(member)

  return (
    <Card
      size="small"
      className={['离岗', '休息'].includes(member.effective_status) ? 'bg-slate-50' : ''}
      styles={{ body: { padding: 16 } }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-base">
            <MemberProfileLink member={member} onOpen={onViewProfile} />
            <Tag color="blue" className="m-0">
              {member.position || '组员'}
            </Tag>
          </div>
          <div className="mt-1 truncate text-sm text-slate-500">
            {member.departments?.map(item => item.name).join('、') || member.department?.name || '未分配部门'}
          </div>
        </div>
        <Tag color={color} className="m-0 shrink-0">
          {label}
        </Tag>
      </div>

      {(detail || reason) && (
        <div className="mt-3 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs leading-5">
          {detail && <div className="text-slate-600">{detail}</div>}
          {reason && <div className="text-slate-500">原因：{reason}</div>}
        </div>
      )}

      <div className="mt-3 space-y-2 rounded-lg bg-slate-100/70 px-3 py-2.5 text-sm">
        <div className="flex min-w-0 gap-3">
          <span className="w-16 shrink-0 text-slate-500">电话</span>
          <span className="min-w-0 truncate text-slate-700" title={member.phone || '-'}>
            {member.phone || '-'}
          </span>
        </div>
        {canManageIdentity && <div className="flex min-w-0 gap-3">
          <span className="w-16 shrink-0 text-slate-500">身份证</span>
          <span
            className="min-w-0 truncate text-slate-700"
            title={member.id_card_number || '未补齐'}
          >
            {member.id_card_number || '未补齐'}
          </span>
        </div>}
        {canViewSensitive && member.notes && (
          <div className="flex min-w-0 gap-3">
            <span className="w-16 shrink-0 text-slate-500">备注</span>
            <span className="min-w-0 truncate text-slate-700" title={member.notes}>
              {member.notes}
            </span>
          </div>
        )}
      </div>

      {(canManage || canManageAttendance || canDelete) && <div className="mt-4 flex gap-2 border-t border-slate-200 pt-3">
        {canManage && <Button block icon={<EditOutlined />} onClick={onEdit}>
          编辑
        </Button>}
        {canManageAttendance && <Button block icon={<CalendarOutlined />} onClick={onLeave}>
          请假
        </Button>}
        {canDelete && <Button block danger icon={<DeleteOutlined />} onClick={onDelete}>
          删除
        </Button>}
      </div>}
    </Card>
  )
}

function MemberForm({
  member,
  departments,
  accountOptions,
  canManageIdentity,
  onClose,
  onSaved,
}: {
  member: GridMember | null
  departments: DepartmentOption[]
  accountOptions: AccountOption[]
  canManageIdentity: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(member?.name || '')
  const [departmentIds, setDepartmentIds] = useState<number[]>(
    member?.department_ids || (member?.department_id ? [member.department_id] : []),
  )
  const [position, setPosition] = useState<PersonnelPosition>(
    (member?.position as PersonnelPosition) || '组员',
  )
  const [phone, setPhone] = useState(member?.phone || '')
  const [idCardNumber, setIdCardNumber] = useState(member?.id_card_number || '')
  const [notes, setNotes] = useState(member?.notes || '')
  const [accountMode, setAccountMode] = useState<'existing' | 'create'>('existing')
  const [existingUserId, setExistingUserId] = useState<number | null>(null)
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(
    member?.account?.id || null,
  )
  const [editAccountOptions, setEditAccountOptions] = useState<AccountOption[]>([])
  const [accountOptionsLoading, setAccountOptionsLoading] = useState(Boolean(member))
  const [accountOptionsError, setAccountOptionsError] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const internalPosition = ['片长', '中队长', '基础管控', '所队领导'].includes(position)
  const multipleCommunities = position === '社区民警'
  const communityPosition = ['组长', '组员', '社区民警'].includes(position)
  const currentAccountId = member?.account?.id || null

  useEffect(() => {
    if (!member) return
    let cancelled = false
    setAccountOptionsLoading(true)
    getMemberAccountOptions(member.id)
      .then(options => {
        if (!cancelled) {
          setEditAccountOptions(options)
          setAccountOptionsError('')
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAccountOptionsError('账号列表加载失败，仍可保存其他资料')
        }
      })
      .finally(() => {
        if (!cancelled) setAccountOptionsLoading(false)
      })
    return () => { cancelled = true }
  }, [member])

  const handleSave = async () => {
    if (!name.trim()) {
      setFormError('姓名不能为空')
      return
    }
    if (communityPosition && departmentIds.length === 0) {
      setFormError('该岗位必须选择社区部门')
      return
    }
    if (!multipleCommunities && departmentIds.length > 1) {
      setFormError('该岗位只能选择一个社区部门')
      return
    }
    if (!member && accountMode === 'existing' && !existingUserId) {
      setFormError('请选择要关联的已有账号')
      return
    }
    if (!member && accountMode === 'create' && (!username.trim() || password.length < 8)) {
      setFormError('请输入用户名和至少 8 个字符的初始密码')
      return
    }
    const normalizedIdentity = idCardNumber.replace(/\s+/g, '').toUpperCase()
    if (
      canManageIdentity
      && normalizedIdentity
      && !/^(?:\d{15}|\d{17}[\dX])$/.test(normalizedIdentity)
    ) {
      setFormError('身份证号必须是有效的 15 位或 18 位号码')
      return
    }
    setSaving(true)
    setFormError('')
    try {
      const payload: {
        department_ids: number[]
        position: PersonnelPosition
        phone: string
        notes: string
        id_card_number?: string | null
        account_id?: number
      } = {
        department_ids: departmentIds,
        position,
        phone,
        notes,
      }
      if (canManageIdentity) {
        const initialIdentity = (member?.id_card_number || '').replace(/\s+/g, '').toUpperCase()
        if (!member || normalizedIdentity !== initialIdentity) {
          payload.id_card_number = normalizedIdentity || null
        }
      }
      if (member) {
        if (selectedAccountId && selectedAccountId !== currentAccountId) {
          payload.account_id = selectedAccountId
        }
        await updateGridMember(member.id, payload)
      } else {
        await createGridMember({
          name: name.trim(),
          ...payload,
          account_mode: accountMode,
          existing_user_id: accountMode === 'existing' ? existingUserId : null,
          username: accountMode === 'create' ? username.trim() : undefined,
          password: accountMode === 'create' ? password : undefined,
        })
      }
      onSaved()
    } catch (error: any) {
      const detail = error?.response?.data?.detail
      setFormError(
        typeof detail === 'object' ? detail?.message || '保存失败' : detail || '保存失败',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open
      title={member ? '编辑人员' : '添加人员'}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      maskClosable={!saving}
      width={580}
      onOk={handleSave}
      onCancel={onClose}
    >
      <div className="space-y-4 pt-2">
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            姓名
          </label>
          <Input
            value={name}
            onChange={event => setName(event.target.value)}
            disabled={!!member}
          />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            所属部门
          </label>
          {multipleCommunities ? (
            <Select
              mode="multiple"
              value={departmentIds}
              onChange={setDepartmentIds}
              placeholder="可选择一个或多个社区"
              className="w-full"
              maxTagCount="responsive"
              options={departments
                .filter(item => item.type === 'community' && item.is_active)
                .map(item => ({ value: item.id, label: item.name }))}
            />
          ) : (
            <Select
              value={departmentIds[0]}
              onChange={value => setDepartmentIds(value ? [value] : [])}
              allowClear={!internalPosition && !communityPosition}
              placeholder="请选择部门"
              className="w-full"
              disabled={internalPosition}
              options={departments
                .filter(item => internalPosition
                  ? item.type === 'internal'
                  : item.type === 'community' && item.is_active)
                .map(item => ({ value: item.id, label: item.name }))}
            />
          )}
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            岗位
          </label>
          <Select
            value={position}
            onChange={(value: PersonnelPosition) => {
              setPosition(value)
              const nextIsInternal = ['片长', '中队长', '基础管控', '所队领导'].includes(value)
              const currentDepartment = departments.find(item => item.id === departmentIds[0])
              if (nextIsInternal) {
                const internalId = departments.find(item => item.type === 'internal')?.id
                setDepartmentIds(internalId ? [internalId] : [])
              } else if (currentDepartment?.type === 'internal') {
                setDepartmentIds([])
              } else if (value !== '社区民警' && departmentIds.length > 1) {
                setDepartmentIds(departmentIds.slice(0, 1))
              }
            }}
            className="w-full"
            options={PERSONNEL_POSITIONS.map(item => ({
              value: item,
              label: item,
            }))}
          />
          <p className="mt-1.5 text-xs text-slate-500">
            <span className="flex items-start gap-1.5 leading-5">
              <InfoCircleOutlined className="mt-0.5 shrink-0" />
              <span>
                在线汇总固定统计有社区部门的组长和组员；社区民警可以同时选择多个社区，其他社区岗位只能选择一个，内勤岗位自动归入内勤。
              </span>
            </span>
          </p>
        </div>
        {!member && <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
          <div className="mb-2 font-medium text-slate-700">登录账号</div>
          <Radio.Group
            value={accountMode}
            onChange={event => setAccountMode(event.target.value)}
            options={[
              { value: 'existing', label: '关联已有账号' },
              { value: 'create', label: '同时创建账号' },
            ]}
          />
          {accountMode === 'existing' ? (
            <Select
              showSearch
              optionFilterProp="label"
              value={existingUserId || undefined}
              onChange={setExistingUserId}
              placeholder="请选择尚未关联人员的账号"
              className="mt-3 w-full"
              options={accountOptions.map(account => ({
                value: account.id,
                label: `${account.display_name}（${account.username_masked}）`,
              }))}
            />
          ) : (
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <Input
                value={username}
                onChange={event => setUsername(event.target.value)}
                placeholder="登录用户名"
                autoComplete="off"
              />
              <Input.Password
                value={password}
                onChange={event => setPassword(event.target.value)}
                placeholder="至少 8 位初始密码"
                autoComplete="new-password"
              />
            </div>
          )}
          <p className="mt-2 text-xs leading-5 text-slate-500">
            账号姓名与人员姓名一致，默认继承该岗位的权限组；新账号会标记为临时密码。
          </p>
        </div>}
        {member && <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
          <div className="mb-2 font-medium text-slate-700">关联账号</div>
          <Select
            showSearch
            optionFilterProp="label"
            value={selectedAccountId || undefined}
            onChange={setSelectedAccountId}
            loading={accountOptionsLoading}
            placeholder="请选择关联账号"
            className="w-full"
            options={editAccountOptions.map(account => ({
              value: account.id,
              label: `${account.display_name}（${account.username_masked}）${
                account.is_current
                  ? ' · 当前关联'
                  : account.linked_member_id
                    ? ` · 将与${account.linked_member_name}互换`
                    : ' · 空闲账号'
              }`,
            }))}
          />
          {accountOptionsError && (
            <p className="mt-2 text-xs text-red-700">{accountOptionsError}</p>
          )}
          {selectedAccountId && selectedAccountId !== currentAccountId && (
            <Alert
              className="mt-3"
              type="warning"
              showIcon
              message={editAccountOptions.find(item => item.id === selectedAccountId)?.linked_member_id
                ? '保存后两名人员的账号将互换'
                : '保存后当前账号将释放，并改绑到所选账号'}
              description="受影响账号会退出当前登录，并按新人员岗位重新继承权限。"
            />
          )}
          <p className="mt-2 text-xs leading-5 text-slate-500">
            人员和账号保持一对一；用户管理中仍不能单独解除或改绑。
          </p>
        </div>}
        {canManageIdentity && <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            身份证号
          </label>
          <Input
            value={idCardNumber}
            onChange={event => setIdCardNumber(event.target.value)}
            placeholder="15 位或 18 位，可留空"
            maxLength={18}
            autoComplete="off"
          />
          <p className="mt-1.5 text-xs text-slate-500">
            仅超级管理员可以查看、填写、替换或清空。
          </p>
        </div>}
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            电话
          </label>
          <Input value={phone} onChange={event => setPhone(event.target.value)} />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            备注
          </label>
          <Input.TextArea
            value={notes}
            onChange={event => setNotes(event.target.value)}
            rows={3}
          />
        </div>
        {formError && <p className="text-sm text-red-700">{formError}</p>}
      </div>
    </Modal>
  )
}

function LeaveModal({
  member,
  onClose,
  onSaved,
}: {
  member: GridMember
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const [mode, setMode] = useState<'temporary' | 'long_term'>(
    member.status === '离岗' ? 'long_term' : 'temporary',
  )
  const [leaveRange, setLeaveRange] = useState<[string, string] | null>(
    member.leave_start_date && member.leave_end_date
      ? [member.leave_start_date, member.leave_end_date]
      : null,
  )
  const [reason, setReason] = useState(member.leave_reason || '')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const hasLeave = (
    member.status === '离岗'
    || Boolean(member.leave_start_date && member.leave_end_date)
  )

  const handleSave = async () => {
    if (
      mode === 'temporary'
      && (!leaveRange?.[0] || !leaveRange?.[1])
    ) {
      setFormError('请选择临时请假日期')
      return
    }
    setSaving(true)
    setFormError('')
    try {
      await updateGridMemberLeave(member.id, {
        action: mode,
        leave_start_date: mode === 'temporary' ? leaveRange?.[0] : null,
        leave_end_date: mode === 'temporary' ? leaveRange?.[1] : null,
        leave_reason: reason,
      })
      onSaved(mode === 'long_term' ? '已设置为长期' : '请假日期已保存')
    } catch (error: any) {
      setFormError(error?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleClear = async () => {
    setSaving(true)
    setFormError('')
    try {
      await updateGridMemberLeave(member.id, { action: 'clear' })
      onSaved('已恢复正常')
    } catch (error: any) {
      setFormError(error?.response?.data?.detail || '恢复失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open
      title={`${member.name} · 请假设置`}
      className="leave-settings-modal"
      maskClosable={!saving}
      onCancel={onClose}
      styles={{
        body: {
          maxHeight: 'calc(100dvh - 190px)',
          overflowY: 'auto',
        },
      }}
      footer={(
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <Button
            className="min-h-11 w-full sm:min-h-0 sm:w-auto"
            disabled={!hasLeave}
            loading={saving}
            onClick={handleClear}
          >
            恢复正常
          </Button>
          <div className="grid grid-cols-2 gap-2 sm:flex">
            <Button className="min-h-11 sm:min-h-0" disabled={saving} onClick={onClose}>取消</Button>
            <Button className="min-h-11 sm:min-h-0" type="primary" loading={saving} onClick={handleSave}>
              保存
            </Button>
          </div>
        </div>
      )}
    >
      <div className="space-y-4 pt-2">
        <div>
          <label className="mb-2 block text-sm font-medium text-slate-700">
            类型
          </label>
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={mode}
            onChange={event => setMode(event.target.value)}
            options={[
              { value: 'temporary', label: '临时请假' },
              { value: 'long_term', label: '长期' },
            ]}
          />
        </div>

        {mode === 'temporary' ? (
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">
              请假日期
            </label>
            <div className="grid grid-cols-2 gap-2 md:hidden">
              <label className="min-w-0 text-xs text-slate-500">
                <span className="mb-1 block">开始日期</span>
                <input
                  aria-label="请假开始日期"
                  className="h-11 w-full min-w-0 rounded-lg border border-slate-300 bg-white px-2 text-sm text-slate-800 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
                  type="date"
                  value={leaveRange?.[0] || ''}
                  onChange={event => {
                    const nextStart = event.target.value
                    if (!nextStart) {
                      setLeaveRange(null)
                      return
                    }
                    const currentEnd = leaveRange?.[1] || nextStart
                    setLeaveRange([
                      nextStart,
                      currentEnd < nextStart ? nextStart : currentEnd,
                    ])
                  }}
                />
              </label>
              <label className="min-w-0 text-xs text-slate-500">
                <span className="mb-1 block">结束日期</span>
                <input
                  aria-label="请假结束日期"
                  className="h-11 w-full min-w-0 rounded-lg border border-slate-300 bg-white px-2 text-sm text-slate-800 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
                  min={leaveRange?.[0] || undefined}
                  type="date"
                  value={leaveRange?.[1] || ''}
                  onChange={event => {
                    const nextEnd = event.target.value
                    const currentStart = leaveRange?.[0] || nextEnd
                    if (!nextEnd || !currentStart) {
                      setLeaveRange(null)
                      return
                    }
                    setLeaveRange([
                      currentStart,
                      nextEnd < currentStart ? currentStart : nextEnd,
                    ])
                  }}
                />
              </label>
            </div>
            <div className="hidden md:block">
              <DatePicker.RangePicker
                value={leaveRange
                  ? [dayjs(leaveRange[0]), dayjs(leaveRange[1])]
                  : null}
                onChange={(_, dateStrings) => {
                  setLeaveRange(
                    dateStrings[0] && dateStrings[1]
                      ? [dateStrings[0], dateStrings[1]]
                      : null,
                  )
                }}
                format="YYYY-MM-DD"
                placeholder={['开始日期', '结束日期']}
                className="w-full"
              />
            </div>
            <p className="mt-1.5 text-xs text-slate-500">
              日期结束后会自动恢复正常。
            </p>
          </div>
        ) : (
          <Alert
            type="info"
            showIcon
            message="长期没有结束日期"
            description="人员恢复工作时，再打开此窗口点击“恢复正常”。"
          />
        )}

        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            原因或说明
          </label>
          <Input
            value={reason}
            onChange={event => setReason(event.target.value)}
            maxLength={200}
            placeholder={mode === 'temporary' ? '例如：年假、病假' : '例如：长期借调'}
          />
        </div>
        {formError && <p className="text-sm text-red-700">{formError}</p>}
      </div>
    </Modal>
  )
}
