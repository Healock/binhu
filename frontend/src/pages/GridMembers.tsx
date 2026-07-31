import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
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
  listGridMembers,
  updateGridMember,
  updateGridMemberLeave,
  type GridCommunity,
  type GridMember,
  type AttendanceHistoryItem,
  type AttendanceScheduleStatus,
  type DepartmentOption,
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
  const [members, setMembers] = useState<GridMember[]>([])
  const [communities, setCommunities] = useState<GridCommunity[]>([])
  const [departments, setDepartments] = useState<DepartmentOption[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [communityFilter, setCommunityFilter] = useState('')
  const [positionFilter, setPositionFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<GridMember | null>(null)
  const [leaveEditing, setLeaveEditing] = useState<GridMember | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyRows, setHistoryRows] = useState<AttendanceHistoryItem[]>([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [showAddForm, setShowAddForm] = useState(false)
  const [msg, setMsg] = useState('')
  const [loadError, setLoadError] = useState('')
  const [scheduleRange, setScheduleRange] = useState<[string, string]>(() => [
    dayjs().startOf('month').format('YYYY-MM-DD'),
    dayjs().endOf('month').format('YYYY-MM-DD'),
  ])
  const [scheduleStatus, setScheduleStatus] = useState<AttendanceScheduleStatus | null>(null)
  const [scheduleLoading, setScheduleLoading] = useState(false)
  const [scheduleError, setScheduleError] = useState('')
  const pageSize = 100
  const canManage = Boolean(user?.permissions.includes('personnel.manage'))
  const canManageAttendance = Boolean(user?.permissions.includes('attendance.manage'))
  const canViewSensitive = Boolean(user?.permissions.includes('personnel.sensitive.view'))

  const fetch = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const response = await listGridMembers({
        keyword: keyword || undefined,
        community: communityFilter || undefined,
        position: positionFilter || undefined,
        page,
        page_size: pageSize,
      })
      setMembers(response.data)
      setTotal(response.total)
    } catch {
      setLoadError('人员列表加载失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [keyword, communityFilter, positionFilter, page, pageSize])

  const fetchCommunities = useCallback(async () => {
    try {
      const [communityRows, departmentRows] = await Promise.all([
        getGridCommunities(),
        getDepartments(),
      ])
      setCommunities(communityRows)
      setDepartments(departmentRows)
    } catch {
      // 人员列表仍然可以独立显示。
    }
  }, [])

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
    fetch()
    fetchCommunities()
  }

  const handleDelete = (id: number, name: string) => {
    Modal.confirm({
      title: '删除人员',
      content: `确认删除人员“${name}”？`,
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
  const normalCount = members.filter(
    member => member.effective_status === '在岗',
  ).length
  const memberColumns: TableColumnsType<GridMember> = [
    {
      title: '姓名',
      dataIndex: 'name',
      key: 'name',
      width: 110,
      render: value => (
        <span className="font-medium text-slate-800">{value}</span>
      ),
    },
    {
      title: '所属部门',
      key: 'department',
      width: 150,
      render: (_, member) => `${member.department?.name || '未分配部门'} · ${member.position}`,
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
      dataIndex: 'id_card_masked',
      key: 'id_card_masked',
      width: 185,
      render: (value, member) => (
        member.has_id_card
          ? (
              <Tooltip title="完整号码仅保存在数据库中，页面不显示">
                <span>{value}</span>
              </Tooltip>
            )
          : <span className="text-slate-400">未补齐</span>
      ),
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
          {canManage && <Button
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
        description="选择一个日期区间，集中查看哪些周还没有完成组长和组员排班"
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
              message="所选区间还有双休日未排班"
              description={`请补齐这些周的排班：${scheduleStatus.missing_week_starts.join('、')}。补齐前，走访汇总中的人均日数据不会显示。`}
              action={(
                <Button
                  size="small"
                  onClick={() => navigate(
                    `/grid-members/weekend-duty?week=${scheduleStatus.missing_week_starts[0]}`,
                  )}
                >
                  去补排班
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
              setPage(1)
            }}
            className="w-full md:min-w-56 md:flex-1"
          />
          <Select
            value={communityFilter}
            onChange={value => {
              setCommunityFilter(value)
              setPage(1)
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
              setPage(1)
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
              setPage(1)
            }}
          >
            搜索
          </Button>
          <div className="flex w-full flex-wrap gap-2 md:ml-auto md:w-auto">
            <Tag color="blue">共 {total} 人</Tag>
            <Tag color="green">当前页正常 {normalCount} 人</Tag>
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

      <div className="hidden md:block">
        <AppTable<GridMember>
          columns={memberColumns.filter(column => (
            canViewSensitive || !['phone', 'id_card_masked', 'notes'].includes(String(column.key))
          ))}
          dataSource={members}
          emptyText={loadError || '暂无人员，可点击“添加人员”手动添加'}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            hideOnSinglePage: true,
            showSizeChanger: false,
            showTotal: count => `共 ${count} 人`,
            onChange: setPage,
          }}
          rowClassName={member => (
            member.effective_status === '离岗' ? 'app-table-row--muted' : ''
          )}
          rowKey="id"
          scroll={{ x: 1250 }}
        />
      </div>

      <div className="md:hidden">
        {loading ? (
          <Card size="small">
            <Skeleton active paragraph={{ rows: 5 }} />
          </Card>
        ) : members.length > 0 ? (
          <div className="space-y-3">
            {members.map(member => (
              <MobileMemberCard
                key={member.id}
                member={member}
                onEdit={() => setEditing(member)}
                onLeave={() => setLeaveEditing(member)}
                onDelete={() => handleDelete(member.id, member.name)}
                canManage={canManage}
                canManageAttendance={canManageAttendance}
                canViewSensitive={canViewSensitive}
              />
            ))}
          </div>
        ) : (
          <Card size="small">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={loadError || '暂无人员，可点击“添加人员”手动添加'}
            />
          </Card>
        )}

        {!loading && total > pageSize && (
          <div className="mt-4 flex justify-center">
            <Pagination
              simple
              current={page}
              pageSize={pageSize}
              total={total}
              showSizeChanger={false}
              onChange={setPage}
            />
          </div>
        )}
      </div>

      {canManage && (showAddForm || editing) && (
        <MemberForm
          member={editing}
          departments={departments}
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
  const isUpcoming = !isLongTerm && member.leave_state === 'upcoming'
  const label = isLongTerm
    ? '长期离岗'
    : isActiveLeave
    ? '请假中'
    : isUpcoming
    ? '待请假'
    : '正常'
  const color = isLongTerm
    ? 'default'
    : isActiveLeave
    ? 'orange'
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

function MobileMemberCard({
  member,
  onEdit,
  onLeave,
  onDelete,
  canManage,
  canManageAttendance,
  canViewSensitive,
}: {
  member: GridMember
  onEdit: () => void
  onLeave: () => void
  onDelete: () => void
  canManage: boolean
  canManageAttendance: boolean
  canViewSensitive: boolean
}) {
  const { label, color, detail, reason } = getMemberStatusMeta(member)

  return (
    <Card
      size="small"
      className={member.effective_status === '离岗' ? 'bg-slate-50' : ''}
      styles={{ body: { padding: 16 } }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-base font-semibold text-slate-900">
              {member.name}
            </span>
            <Tag color="blue" className="m-0">
              {member.position || '组员'}
            </Tag>
          </div>
          <div className="mt-1 truncate text-sm text-slate-500">
            {member.department?.name || '未分配部门'}
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

      {canViewSensitive && <div className="mt-3 space-y-2 rounded-lg bg-slate-100/70 px-3 py-2.5 text-sm">
        <div className="flex min-w-0 gap-3">
          <span className="w-16 shrink-0 text-slate-500">电话</span>
          <span className="min-w-0 truncate text-slate-700" title={member.phone || '-'}>
            {member.phone || '-'}
          </span>
        </div>
        <div className="flex min-w-0 gap-3">
          <span className="w-16 shrink-0 text-slate-500">身份证</span>
          <span
            className="min-w-0 truncate text-slate-700"
            title={member.has_id_card ? member.id_card_masked : '未补齐'}
          >
            {member.has_id_card ? member.id_card_masked : '未补齐'}
          </span>
        </div>
        {member.notes && (
          <div className="flex min-w-0 gap-3">
            <span className="w-16 shrink-0 text-slate-500">备注</span>
            <span className="min-w-0 truncate text-slate-700" title={member.notes}>
              {member.notes}
            </span>
          </div>
        )}
      </div>}

      {(canManage || canManageAttendance) && <div className="mt-4 flex gap-2 border-t border-slate-200 pt-3">
        {canManage && <Button block icon={<EditOutlined />} onClick={onEdit}>
          编辑
        </Button>}
        {canManageAttendance && <Button block icon={<CalendarOutlined />} onClick={onLeave}>
          请假
        </Button>}
        {canManage && <Button block danger icon={<DeleteOutlined />} onClick={onDelete}>
          删除
        </Button>}
      </div>}
    </Card>
  )
}

function MemberForm({
  member,
  departments,
  onClose,
  onSaved,
}: {
  member: GridMember | null
  departments: DepartmentOption[]
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(member?.name || '')
  const [departmentId, setDepartmentId] = useState<number | null>(
    member?.department_id || null,
  )
  const [position, setPosition] = useState<PersonnelPosition>(
    (member?.position as PersonnelPosition) || '组员',
  )
  const [phone, setPhone] = useState(member?.phone || '')
  const [notes, setNotes] = useState(member?.notes || '')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  const handleSave = async () => {
    if (!name.trim()) {
      setFormError('姓名不能为空')
      return
    }
    setSaving(true)
    setFormError('')
    try {
      const payload = {
        department_id: departmentId,
        position,
        phone,
        notes,
      }
      if (member) {
        await updateGridMember(member.id, payload)
      } else {
        await createGridMember({ name: name.trim(), ...payload })
      }
      onSaved()
    } catch (error: any) {
      setFormError(error?.response?.data?.detail || '保存失败')
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
          <Select
            value={departmentId || undefined}
            onChange={setDepartmentId}
            placeholder="请选择部门"
            className="w-full"
            disabled={['片长', '中队长', '基础管控'].includes(position)}
            options={departments
              .filter(item => item.type === 'community')
              .map(item => ({ value: item.id, label: item.name }))}
          />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">
            岗位
          </label>
          <Select
            value={position}
            onChange={setPosition}
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
                出租房汇总岗位由超级管理员配置；“自购房”岗位进入单独的自购房汇总。
              </span>
            </span>
          </p>
        </div>
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
