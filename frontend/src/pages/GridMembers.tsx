import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  DatePicker,
  Input,
  Modal,
  Radio,
  Select,
  Tag,
  Tooltip,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  DownloadOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import AppTable from '../components/AppTable'
import {
  createGridMember,
  deleteGridMember,
  exportGridMembersUrl,
  getGridCommunities,
  listGridMembers,
  updateGridMember,
  updateGridMemberLeave,
  type GridCommunity,
  type GridMember,
} from '../api/client'
import {
  PERSONNEL_POSITIONS,
  type PersonnelPosition,
} from '../constants/personnel'
import { PageHeader } from '../components/ui'

export default function GridMembers() {
  const [members, setMembers] = useState<GridMember[]>([])
  const [communities, setCommunities] = useState<GridCommunity[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [communityFilter, setCommunityFilter] = useState('')
  const [positionFilter, setPositionFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<GridMember | null>(null)
  const [leaveEditing, setLeaveEditing] = useState<GridMember | null>(null)
  const [showAddForm, setShowAddForm] = useState(false)
  const [msg, setMsg] = useState('')
  const [loadError, setLoadError] = useState('')
  const pageSize = 100

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
      setCommunities(await getGridCommunities())
    } catch {
      // 人员列表仍然可以独立显示。
    }
  }, [])

  useEffect(() => { fetch() }, [fetch])
  useEffect(() => { fetchCommunities() }, [fetchCommunities])

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
      title: '所属社区',
      dataIndex: 'community',
      key: 'community',
      width: 125,
      render: value => value || '-',
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
      title: '请假情况',
      key: 'status',
      width: 180,
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
          <Button type="link" size="small" onClick={() => setEditing(member)}>
            编辑
          </Button>
          <Button type="link" size="small" onClick={() => setLeaveEditing(member)}>
            请假
          </Button>
          <Button
            type="link"
            danger
            size="small"
            onClick={() => handleDelete(member.id, member.name)}
          >
            删除
          </Button>
        </div>
      ),
    },
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="人员管理"
        description="维护人员资料和岗位；请假、长期与恢复正常使用独立按钮"
        actions={(
          <>
            <Button
              icon={<DownloadOutlined />}
              onClick={() => window.open(exportGridMembersUrl(), '_blank')}
            >
              导出 CSV
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setShowAddForm(true)}
            >
              添加人员
            </Button>
          </>
        )}
      />

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
            className="min-w-56 flex-1"
          />
          <Select
            value={communityFilter}
            onChange={value => {
              setCommunityFilter(value)
              setPage(1)
            }}
            className="min-w-36"
            options={[
              { value: '', label: '全部社区' },
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
            className="min-w-36"
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
            onClick={() => {
              setKeyword(searchInput)
              setPage(1)
            }}
          >
            搜索
          </Button>
          <div className="ml-auto flex gap-2">
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

      <AppTable<GridMember>
        columns={memberColumns}
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

      {(showAddForm || editing) && (
        <MemberForm
          member={editing}
          communities={communityNames}
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
    </div>
  )
}

function MemberStatus({ member }: { member: GridMember }) {
  const isLongTerm = member.status === '离岗'
  const isActiveLeave = !isLongTerm && member.effective_status === '离岗'
  const isUpcoming = !isLongTerm && member.leave_state === 'upcoming'
  const label = isLongTerm
    ? '长期'
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

  return (
    <div className="min-w-32">
      <Tag color={color}>{label}</Tag>
      {member.status_detail && (
        <div className="mt-1 text-xs text-slate-500">
          {member.status_detail}
        </div>
      )}
      {member.leave_reason && (isLongTerm || isActiveLeave || isUpcoming) && (
        <div
          className="mt-0.5 max-w-48 truncate text-xs text-slate-400"
          title={member.leave_reason}
        >
          {member.leave_reason}
        </div>
      )}
    </div>
  )
}

function MemberForm({
  member,
  communities,
  onClose,
  onSaved,
}: {
  member: GridMember | null
  communities: string[]
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(member?.name || '')
  const [community, setCommunity] = useState(member?.community || '')
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
        community,
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
            所属社区
          </label>
          <Select
            value={community || undefined}
            onChange={setCommunity}
            placeholder="请选择社区"
            className="w-full"
            options={communities.map(item => ({ value: item, label: item }))}
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
            是否进入在线汇总和走访汇总，由超级管理员在系统设置中决定。
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
    if (mode === 'temporary' && !leaveRange) {
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
      maskClosable={!saving}
      onCancel={onClose}
      footer={(
        <div className="flex justify-between">
          <Button
            disabled={!hasLeave}
            loading={saving}
            onClick={handleClear}
          >
            恢复正常
          </Button>
          <div className="flex gap-2">
            <Button disabled={saving} onClick={onClose}>取消</Button>
            <Button type="primary" loading={saving} onClick={handleSave}>
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
