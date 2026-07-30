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
import { useNavigate, useSearchParams } from 'react-router-dom'
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
  draggable,
  selectable,
  onToggle,
  onAssign,
}: {
  member: WeekendDutyMember
  selected: boolean
  editable: boolean
  draggable: boolean
  selectable: boolean
  onToggle: () => void
  onAssign: (target: DutyTarget) => void
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    isDragging,
  } = useDraggable({
    id: `member:${member.id}`,
    disabled: !editable || member.exempt || !draggable,
  })
  return (
    <div
      ref={setNodeRef}
      {...(draggable ? attributes : {})}
      {...(draggable ? listeners : {})}
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
        draggable && editable && !member.exempt
          ? 'cursor-grab touch-none active:cursor-grabbing'
          : '',
      ].join(' ')}
    >
      <div>
        <button
          type="button"
          className="w-full min-w-0 text-left"
          disabled={!editable || member.exempt}
          tabIndex={selectable ? 0 : -1}
          aria-disabled={!selectable || !editable || member.exempt}
          onClick={selectable ? onToggle : undefined}
        >
          <span className="block truncate font-medium text-slate-800">
            {member.name}
          </span>
          <span className="mt-0.5 block truncate text-xs text-slate-500">
            {member.community} · {member.position}
          </span>
        </button>
      </div>
      {draggable && editable && !member.exempt && (
        <div className="mt-1 hidden text-[11px] text-slate-400 md:block">
          拖动整张卡片调整分组，点击姓名可批量选择
        </div>
      )}
      {member.exempt ? (
        <div className="mt-2">
          <Tag>周末请假，无需排班</Tag>
        </div>
      ) : (
        <div className="mt-2 grid grid-cols-3 gap-2 md:hidden">
          <Button
            size="small"
            type={member.assignment === 'saturday' ? 'primary' : 'default'}
            disabled={!editable || member.unavailable_days.includes('saturday')}
            onClick={() => onAssign('saturday')}
            className="min-h-11"
          >
            周六
          </Button>
          <Button
            size="small"
            type={member.assignment === 'sunday' ? 'primary' : 'default'}
            disabled={!editable || member.unavailable_days.includes('sunday')}
            onClick={() => onAssign('sunday')}
            className="min-h-11"
          >
            周日
          </Button>
          <Button
            size="small"
            type={!member.assignment && member.recorded ? 'primary' : 'default'}
            disabled={!editable}
            onClick={() => onAssign('unassigned')}
            className="min-h-11"
          >
            双休
          </Button>
        </div>
      )}
      {!member.exempt && !member.assignment && member.recorded && (
        <div className="mt-2 text-xs text-slate-500">
          已确认周六、周日都休息
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
  draggable,
  selectable,
  onToggle,
  onAssign,
}: {
  id: DutyTarget
  title: string
  subtitle: string
  members: WeekendDutyMember[]
  selected: Set<number>
  editable: boolean
  draggable: boolean
  selectable: boolean
  onToggle: (id: number) => void
  onAssign: (id: number, target: DutyTarget) => void
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
            draggable={draggable}
            selectable={selectable}
            onToggle={() => onToggle(member.id)}
            onAssign={day => onAssign(member.id, day)}
          />
        )) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={id === 'unassigned'
              ? '本周都已安排'
              : draggable ? '可拖动人员到这里' : '使用卡片按钮安排'}
          />
        )}
      </div>
    </section>
  )
}

export default function WeekendDuty() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { user } = useAuth()
  const editable = user?.role === 'admin' || user?.role === 'super_admin'
  const isSuperAdmin = user?.role === 'super_admin'
  const [isMobile, setIsMobile] = useState(() => (
    typeof window !== 'undefined'
    && window.matchMedia('(max-width: 767px)').matches
  ))
  const [weekStart, setWeekStart] = useState(() => {
    const requestedWeek = searchParams.get('week')
    return requestedWeek && dayjs(requestedWeek).isValid()
      ? mondayOf(dayjs(requestedWeek))
      : mondayOf()
  })
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

  useEffect(() => {
    const media = window.matchMedia('(max-width: 767px)')
    const update = () => setIsMobile(media.matches)
    update()
    if (media.addEventListener) {
      media.addEventListener('change', update)
      return () => media.removeEventListener('change', update)
    }
    media.addListener(update)
    return () => media.removeListener(update)
  }, [])
  useEffect(() => {
    if (isMobile) setSelected(new Set())
  }, [isMobile])

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
  const pendingCount = members.filter(item => (
    !item.exempt && !item.assignment && !item.recorded
  )).length
  const restCount = members.filter(item => (
    !item.exempt && !item.assignment && item.recorded
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
        recorded: true,
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
      return { ...member, assignment, recorded: true }
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
    if (!board) return
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
      setError(detail || '保存失败，请检查人员名单或请假冲突')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="app-page">
      <PageHeader
        title="双休日备勤"
        description="为系统设置中选定的备勤岗位安排周六或周日一天在岗；请假日期优先"
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
          <div className="hidden md:block">
            <DatePicker
              picker="week"
              allowClear={false}
              value={dayjs(weekStart)}
              onChange={value => value && setWeekStart(mondayOf(value))}
            />
          </div>
          <input
            type="date"
            value={weekStart}
            aria-label="选择双休日所在周"
            onChange={event => {
              if (event.target.value) {
                setWeekStart(mondayOf(dayjs(event.target.value)))
              }
            }}
            className="min-h-11 min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 text-sm md:hidden"
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
            <Tag color={pendingCount ? 'warning' : 'success'}>
              {pendingCount ? `待确认 ${pendingCount} 人` : '已确认全部人员'}
            </Tag>
            {restCount > 0 && <Tag>两天休息 {restCount} 人</Tag>}
            {exemptCount > 0 && <Tag>请假免排 {exemptCount} 人</Tag>}
          </div>
        </div>
        {board && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg bg-slate-50 px-3 py-2 text-sm">
            <span className="text-slate-500">当前排班岗位</span>
            {board.positions.map(position => (
              <Tag key={position} color="blue">{position}</Tag>
            ))}
            {isSuperAdmin && (
              <Button
                type="link"
                size="small"
                className="ml-auto"
                onClick={() => navigate('/settings/system')}
              >
                修改岗位
              </Button>
            )}
          </div>
        )}

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
                className="hidden md:inline-flex"
              >
                沿用上周
              </Button>
              <Button
                icon={<RetweetOutlined />}
                onClick={swapAll}
                className="hidden md:inline-flex"
              >
                周六周日对调
              </Button>
              {selected.size > 0 && (
                <div className="hidden flex-wrap gap-2 md:flex">
                  <Button onClick={() => assignMembers(selected, 'saturday')}>
                    选中人员排周六
                  </Button>
                  <Button onClick={() => assignMembers(selected, 'sunday')}>
                    选中人员排周日
                  </Button>
                  <Button onClick={() => assignMembers(selected, 'unassigned')}>
                    移回待安排
                  </Button>
                </div>
              )}
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={saving}
                disabled={!board}
                className="md:ml-auto"
                onClick={save}
              >
                保存本周排班
              </Button>
            </>
          )}
        </div>

        {editable && pendingCount > 0 && (
          <Alert
            showIcon
            type="info"
            message={`保存时，仍在待安排区的 ${pendingCount} 人将记为周六、周日都休息`}
          />
        )}
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
              title="待安排 / 两天休息"
              subtitle="未选择人员在保存后记为周六、周日都休息"
              members={groups.unassigned}
              selected={selected}
              editable={editable}
              draggable={!isMobile}
              selectable={!isMobile}
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
              draggable={!isMobile}
              selectable={!isMobile}
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
              draggable={!isMobile}
              selectable={!isMobile}
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
