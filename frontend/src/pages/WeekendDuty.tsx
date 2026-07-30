import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  CalendarOutlined,
  CheckOutlined,
  LeftOutlined,
  RetweetOutlined,
  RightOutlined,
  SaveOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  DatePicker,
  Empty,
  Input,
  Select,
  Skeleton,
  Tag,
} from 'antd'
import dayjs from 'dayjs'
import { useNavigate } from 'react-router-dom'
import {
  getWeekendDuty,
  saveWeekendDuty,
  type WeekendDutyBoard,
  type WeekendDutyDay,
  type WeekendDutyMember,
} from '../api/client'
import { PageHeader } from '../components/ui'
import { useAuth } from '../context/AuthContext'

type DutyTarget = WeekendDutyDay | 'unassigned'

function mondayOf(value = dayjs()) {
  return value.subtract((value.day() + 6) % 7, 'day').format('YYYY-MM-DD')
}

function DutyMemberCard({
  member,
  selected,
  editable,
  onToggle,
  onAssign,
}: {
  member: WeekendDutyMember
  selected: boolean
  editable: boolean
  onToggle: () => void
  onAssign: (day: WeekendDutyDay) => void
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    isDragging,
  } = useDraggable({
    id: `member:${member.id}`,
    disabled: !editable || member.exempt,
  })
  return (
    <div
      ref={setNodeRef}
      style={{
        transform: transform
          ? `translate3d(${transform.x}px, ${transform.y}px, 0)`
          : undefined,
        opacity: isDragging ? 0.55 : 1,
      }}
      className={[
        'rounded-xl border bg-white p-3 shadow-sm transition-colors',
        selected ? 'border-blue-500 ring-2 ring-blue-100' : 'border-slate-200',
        member.exempt ? 'bg-slate-50 opacity-75' : '',
      ].join(' ')}
    >
      <div className="flex items-start gap-2">
        <button
          type="button"
          className="min-w-0 flex-1 text-left"
          disabled={!editable || member.exempt}
          onClick={onToggle}
        >
          <span className="block truncate font-medium text-slate-800">
            {member.name}
          </span>
          <span className="mt-0.5 block truncate text-xs text-slate-500">
            {member.community} · {member.position}
          </span>
        </button>
        {editable && !member.exempt && (
          <button
            type="button"
            aria-label={`拖动${member.name}`}
            className="cursor-grab rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-100 active:cursor-grabbing"
            {...attributes}
            {...listeners}
          >
            ⋮⋮
          </button>
        )}
      </div>
      {member.exempt ? (
        <div className="mt-2">
          <Tag>周末请假，无需排班</Tag>
        </div>
      ) : (
        <div className="mt-2 grid grid-cols-2 gap-2 md:hidden">
          <Button
            size="small"
            type={member.assignment === 'saturday' ? 'primary' : 'default'}
            disabled={!editable || member.unavailable_days.includes('saturday')}
            onClick={() => onAssign('saturday')}
          >
            周六
          </Button>
          <Button
            size="small"
            type={member.assignment === 'sunday' ? 'primary' : 'default'}
            disabled={!editable || member.unavailable_days.includes('sunday')}
            onClick={() => onAssign('sunday')}
          >
            周日
          </Button>
        </div>
      )}
      {member.unavailable_days.length > 0 && !member.exempt && (
        <div className="mt-2 text-xs text-amber-700">
          {member.unavailable_days.includes('saturday') ? '周六请假' : '周日请假'}
          {member.absence_reason ? ` · ${member.absence_reason}` : ''}
        </div>
      )}
    </div>
  )
}

function DutyColumn({
  id,
  title,
  subtitle,
  members,
  selected,
  editable,
  onToggle,
  onAssign,
}: {
  id: DutyTarget
  title: string
  subtitle: string
  members: WeekendDutyMember[]
  selected: Set<number>
  editable: boolean
  onToggle: (id: number) => void
  onAssign: (id: number, day: WeekendDutyDay) => void
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `column:${id}` })
  return (
    <section
      ref={setNodeRef}
      className={[
        'min-h-64 rounded-2xl border p-3',
        isOver ? 'border-blue-500 bg-blue-50' : 'border-slate-200 bg-slate-50',
      ].join(' ')}
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <div>
          <h2 className="font-semibold text-slate-800">{title}</h2>
          <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>
        </div>
        <Tag color={id === 'unassigned' && members.length ? 'warning' : 'blue'}>
          {members.length} 人
        </Tag>
      </div>
      <div className="space-y-2">
        {members.length > 0 ? members.map(member => (
          <DutyMemberCard
            key={member.id}
            member={member}
            selected={selected.has(member.id)}
            editable={editable}
            onToggle={() => onToggle(member.id)}
            onAssign={day => onAssign(member.id, day)}
          />
        )) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={id === 'unassigned' ? '本周都已安排' : '可拖动人员到这里'}
          />
        )}
      </div>
    </section>
  )
}

export default function WeekendDuty() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const editable = user?.role === 'admin' || user?.role === 'super_admin'
  const [weekStart, setWeekStart] = useState(mondayOf())
  const [board, setBoard] = useState<WeekendDutyBoard | null>(null)
  const [members, setMembers] = useState<WeekendDutyMember[]>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [keyword, setKeyword] = useState('')
  const [community, setCommunity] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor),
  )

  const load = useCallback(async (targetWeek: string) => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const next = await getWeekendDuty(targetWeek)
      setBoard(next)
      setMembers(next.members)
      setSelected(new Set())
    } catch {
      setError('双休日备勤加载失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(weekStart) }, [load, weekStart])

  const communities = useMemo(
    () => Array.from(new Set(members.map(item => item.community))).sort(),
    [members],
  )
  const filtered = useMemo(() => members.filter(member => (
    (!keyword || member.name.includes(keyword))
    && (!community || member.community === community)
  )), [community, keyword, members])
  const groups = useMemo(() => ({
    unassigned: filtered.filter(item => !item.exempt && !item.assignment),
    saturday: filtered.filter(item => item.assignment === 'saturday'),
    sunday: filtered.filter(item => item.assignment === 'sunday'),
  }), [filtered])
  const exemptCount = members.filter(item => item.exempt).length
  const unassignedCount = members.filter(item => (
    !item.exempt && !item.assignment
  )).length

  const assignMembers = (ids: Iterable<number>, target: DutyTarget) => {
    const idSet = new Set(ids)
    if (idSet.size === 0) return
    setMembers(current => current.map(member => {
      if (!idSet.has(member.id) || member.exempt) return member
      if (
        target !== 'unassigned'
        && member.unavailable_days.includes(target)
      ) return member
      return {
        ...member,
        assignment: target === 'unassigned' ? null : target,
      }
    }))
    setSelected(new Set())
    setMessage('')
  }

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over) return
    const memberId = Number(String(active.id).replace('member:', ''))
    const target = String(over.id).replace('column:', '') as DutyTarget
    if (!Number.isFinite(memberId)) return
    assignMembers(
      selected.has(memberId) ? selected : [memberId],
      target,
    )
  }

  const copyPrevious = () => {
    setMembers(current => current.map(member => {
      if (member.exempt) return { ...member, assignment: null }
      let assignment = member.previous_assignment
      if (assignment && member.unavailable_days.includes(assignment)) {
        assignment = assignment === 'saturday' ? 'sunday' : 'saturday'
      }
      return { ...member, assignment }
    }))
    setMessage('已沿用上周安排，保存后生效')
  }

  const swapAll = () => {
    setMembers(current => current.map(member => {
      if (member.exempt || !member.assignment) return member
      const swapped = member.assignment === 'saturday' ? 'sunday' : 'saturday'
      if (member.unavailable_days.includes(swapped)) return member
      return { ...member, assignment: swapped }
    }))
    setMessage('已对调可调整的周六、周日人员，保存后生效')
  }

  const save = async () => {
    if (!board || unassignedCount > 0) return
    setSaving(true)
    setError('')
    try {
      const saved = await saveWeekendDuty(
        board.week_start,
        members.map(member => ({
          member_id: member.id,
          duty_day: member.exempt ? null : member.assignment,
        })),
      )
      setBoard(saved)
      setMembers(saved.members)
      setSelected(new Set())
      setMessage('本周双休日备勤已保存')
    } catch (reason) {
      const detail = (
        reason as { response?: { data?: { detail?: string } } }
      ).response?.data?.detail
      setError(detail || '保存失败，请检查是否还有未安排或请假冲突')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="app-page">
      <PageHeader
        title="双休日备勤"
        description="每名组长和组员每周选择周六或周日一天在岗；请假日期优先"
        actions={(
          <Button onClick={() => navigate('/grid-members')}>
            返回人员管理
          </Button>
        )}
      />

      <section className="app-card app-card--padded space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            icon={<LeftOutlined />}
            aria-label="上一周"
            onClick={() => setWeekStart(
              dayjs(weekStart).subtract(7, 'day').format('YYYY-MM-DD'),
            )}
          />
          <DatePicker
            picker="week"
            allowClear={false}
            value={dayjs(weekStart)}
            onChange={value => value && setWeekStart(mondayOf(value))}
          />
          <Button
            icon={<RightOutlined />}
            aria-label="下一周"
            onClick={() => setWeekStart(
              dayjs(weekStart).add(7, 'day').format('YYYY-MM-DD'),
            )}
          />
          {board && (
            <span className="text-sm text-slate-600">
              {board.saturday}（周六）— {board.sunday}（周日）
            </span>
          )}
          <div className="flex w-full flex-wrap gap-2 md:ml-auto md:w-auto">
            <Tag color={unassignedCount ? 'warning' : 'success'}>
              {unassignedCount ? `待安排 ${unassignedCount} 人` : '已全部安排'}
            </Tag>
            {exemptCount > 0 && <Tag>请假免排 {exemptCount} 人</Tag>}
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索姓名"
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
            className="w-full md:w-56"
          />
          <Select
            value={community}
            onChange={setCommunity}
            className="w-full md:w-44"
            options={[
              { value: '', label: '全部社区' },
              ...communities.map(value => ({ value, label: value })),
            ]}
          />
          {editable && (
            <>
              <Button
                icon={<CalendarOutlined />}
                onClick={copyPrevious}
              >
                沿用上周
              </Button>
              <Button icon={<RetweetOutlined />} onClick={swapAll}>
                周六周日对调
              </Button>
              {selected.size > 0 && (
                <>
                  <Button onClick={() => assignMembers(selected, 'saturday')}>
                    选中人员排周六
                  </Button>
                  <Button onClick={() => assignMembers(selected, 'sunday')}>
                    选中人员排周日
                  </Button>
                </>
              )}
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={saving}
                disabled={unassignedCount > 0}
                className="md:ml-auto"
                onClick={save}
              >
                保存本周排班
              </Button>
            </>
          )}
        </div>

        {!editable && (
          <Alert
            showIcon
            type="info"
            message="当前账号只能查看，管理员和超级管理员可以修改排班"
          />
        )}
        {message && (
          <Alert showIcon type="success" icon={<CheckOutlined />} message={message} />
        )}
        {error && <Alert showIcon type="error" message={error} />}
      </section>

      {loading ? (
        <section className="app-card app-card--padded">
          <Skeleton active paragraph={{ rows: 8 }} />
        </section>
      ) : (
        <DndContext sensors={sensors} onDragEnd={handleDragEnd}>
          <div className="grid gap-4 lg:grid-cols-3">
            <DutyColumn
              id="unassigned"
              title="待安排"
              subtitle="先把本周人员分到周六或周日"
              members={groups.unassigned}
              selected={selected}
              editable={editable}
              onToggle={id => setSelected(current => {
                const next = new Set(current)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })}
              onAssign={(id, day) => assignMembers([id], day)}
            />
            <DutyColumn
              id="saturday"
              title="周六备勤"
              subtitle={board?.saturday || ''}
              members={groups.saturday}
              selected={selected}
              editable={editable}
              onToggle={id => setSelected(current => {
                const next = new Set(current)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })}
              onAssign={(id, day) => assignMembers([id], day)}
            />
            <DutyColumn
              id="sunday"
              title="周日备勤"
              subtitle={board?.sunday || ''}
              members={groups.sunday}
              selected={selected}
              editable={editable}
              onToggle={id => setSelected(current => {
                const next = new Set(current)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })}
              onAssign={(id, day) => assignMembers([id], day)}
            />
          </div>
        </DndContext>
      )}
    </div>
  )
}
