import { useEffect, useMemo, useState } from 'react'
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  SortableContext,
  horizontalListSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CloseOutlined,
  HolderOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import { Button, Empty, Tag } from 'antd'
import type {
  MobileDockConfig,
  MobileDockGroupConfig,
  MobileNavigationGroupId,
  MobileNavigationItemId,
  Role,
} from '../types'
import {
  MAX_DOCK_GROUPS,
  accessibleNavigationGroups,
  navigationGroupById,
  navigationItemById,
  normalizeMobileDockConfig,
  reorderMobileDockGroups,
  reorderMobileDockItems,
} from '../navigation/mobileNavigation'
import NavigationIcon from './NavigationIcon'

const GROUP_PREFIX = 'dock-group:'
const GROUP_PALETTE_PREFIX = 'palette-group:'
const ITEM_PREFIX = 'dock-item:'
const ITEM_PALETTE_PREFIX = 'palette-item:'
const GROUP_DROP_ID = 'dock-groups-drop'
const ITEM_DROP_ID = 'dock-items-drop'

function idAfterPrefix(value: string, prefix: string) {
  return value.slice(prefix.length)
}

function PaletteButton({
  id,
  label,
  icon,
  onAdd,
}: {
  id: string
  label: string
  icon: React.ReactNode
  onAdd: () => void
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = (
    useDraggable({ id })
  )
  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Translate.toString(transform),
        opacity: isDragging ? 0.55 : 1,
      }}
      className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white p-2 shadow-sm"
    >
      <button
        type="button"
        className="compact-action dock-config-drag-source flex min-h-10 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-left text-sm text-slate-700"
        {...attributes}
        {...listeners}
      >
        <HolderOutlined className="shrink-0 text-slate-400" />
        <span className="shrink-0 text-blue-600">{icon}</span>
        <span className="truncate">{label}</span>
      </button>
      <Button
        size="small"
        type="text"
        icon={<PlusOutlined />}
        aria-label={`添加${label}`}
        onClick={onAdd}
      />
    </div>
  )
}

function SortableGroup({
  group,
  selected,
  index,
  total,
  onSelect,
  onRemove,
  onMove,
}: {
  group: MobileDockGroupConfig
  selected: boolean
  index: number
  total: number
  onSelect: () => void
  onRemove: () => void
  onMove: (direction: -1 | 1) => void
}) {
  const definition = navigationGroupById(group.id)
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: `${GROUP_PREFIX}${group.id}` })
  if (!definition) return null

  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.6 : 1,
      }}
      className={`dock-config-group${selected ? ' is-selected' : ''}`}
    >
      <button
        type="button"
        className="compact-action dock-config-group__main"
        onClick={onSelect}
      >
        <NavigationIcon name={definition.icon} />
        <span>{definition.dockLabel}</span>
      </button>
      <button
        type="button"
        className="compact-action dock-config-drag-handle"
        aria-label={`拖动${definition.dockLabel}`}
        {...attributes}
        {...listeners}
      >
        <HolderOutlined />
      </button>
      <div className="dock-config-item-actions">
        <button
          type="button"
          className="compact-action"
          disabled={index === 0}
          aria-label={`向前移动${definition.dockLabel}`}
          onClick={() => onMove(-1)}
        >
          <ArrowUpOutlined />
        </button>
        <button
          type="button"
          className="compact-action"
          disabled={index === total - 1}
          aria-label={`向后移动${definition.dockLabel}`}
          onClick={() => onMove(1)}
        >
          <ArrowDownOutlined />
        </button>
        <button
          type="button"
          className="compact-action text-rose-600"
          disabled={total <= 1}
          aria-label={`移除${definition.dockLabel}`}
          onClick={onRemove}
        >
          <CloseOutlined />
        </button>
      </div>
    </div>
  )
}

function SortablePage({
  itemId,
  index,
  total,
  onRemove,
  onMove,
}: {
  itemId: MobileNavigationItemId
  index: number
  total: number
  onRemove: () => void
  onMove: (direction: -1 | 1) => void
}) {
  const item = navigationItemById(itemId)
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: `${ITEM_PREFIX}${itemId}` })
  if (!item) return null

  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.6 : 1,
      }}
      className="dock-config-page"
    >
      <button
        type="button"
        className="compact-action dock-config-drag-handle"
        aria-label={`拖动${item.label}`}
        {...attributes}
        {...listeners}
      >
        <HolderOutlined />
      </button>
      <NavigationIcon name={item.icon} className="text-blue-600" />
      <span className="min-w-0 flex-1 truncate">{item.shortLabel}</span>
      <div className="dock-config-item-actions">
        <button
          type="button"
          className="compact-action"
          disabled={index === 0}
          aria-label={`向前移动${item.label}`}
          onClick={() => onMove(-1)}
        >
          <ArrowUpOutlined />
        </button>
        <button
          type="button"
          className="compact-action"
          disabled={index === total - 1}
          aria-label={`向后移动${item.label}`}
          onClick={() => onMove(1)}
        >
          <ArrowDownOutlined />
        </button>
        <button
          type="button"
          className="compact-action text-rose-600"
          disabled={total <= 1}
          aria-label={`移除${item.label}`}
          onClick={onRemove}
        >
          <CloseOutlined />
        </button>
      </div>
    </div>
  )
}

function GroupDropZone({ children }: { children: React.ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({ id: GROUP_DROP_ID })
  return (
    <div
      ref={setNodeRef}
      className={`dock-config-preview__bar${isOver ? ' is-over' : ''}`}
    >
      {children}
    </div>
  )
}

function ItemDropZone({ children }: { children: React.ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({ id: ITEM_DROP_ID })
  return (
    <div
      ref={setNodeRef}
      className={`dock-config-preview__menu${isOver ? ' is-over' : ''}`}
    >
      {children}
    </div>
  )
}

export default function DockConfigurator({
  value,
  role,
  onChange,
}: {
  value: MobileDockConfig
  role: Role
  onChange: (value: MobileDockConfig) => void
}) {
  const normalized = useMemo(
    () => normalizeMobileDockConfig(value, role),
    [role, value],
  )
  const definitions = useMemo(
    () => accessibleNavigationGroups(role),
    [role],
  )
  const [selectedGroupId, setSelectedGroupId] = (
    useState<MobileNavigationGroupId>(
      normalized.groups[0]?.id || definitions[0].id,
    )
  )
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 6 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  )

  useEffect(() => {
    if (!normalized.groups.some(group => group.id === selectedGroupId)) {
      setSelectedGroupId(normalized.groups[0]?.id || definitions[0].id)
    }
  }, [definitions, normalized.groups, selectedGroupId])

  const selectedGroup = normalized.groups.find(group => (
    group.id === selectedGroupId
  )) || normalized.groups[0]
  const selectedDefinition = selectedGroup
    ? definitions.find(group => group.id === selectedGroup.id)
    : undefined
  const configuredGroups = new Set(normalized.groups.map(group => group.id))
  const availableGroups = definitions.filter(group => (
    !configuredGroups.has(group.id)
  ))
  const configuredItems = new Set(selectedGroup?.items || [])
  const availableItems = (selectedDefinition?.items || []).filter(item => (
    !configuredItems.has(item.id)
  ))

  const commit = (groups: MobileDockGroupConfig[]) => {
    onChange({ groups })
  }

  const addGroup = (
    groupId: MobileNavigationGroupId,
    beforeId?: MobileNavigationGroupId,
  ) => {
    if (
      normalized.groups.length >= MAX_DOCK_GROUPS
      || normalized.groups.some(group => group.id === groupId)
    ) return
    const definition = definitions.find(group => group.id === groupId)
    if (!definition) return
    const nextGroup = {
      id: groupId,
      items: definition.items.map(item => item.id),
    }
    const next = [...normalized.groups]
    const beforeIndex = beforeId
      ? next.findIndex(group => group.id === beforeId)
      : -1
    next.splice(beforeIndex >= 0 ? beforeIndex : next.length, 0, nextGroup)
    commit(next)
    setSelectedGroupId(groupId)
  }

  const addItem = (
    itemId: MobileNavigationItemId,
    beforeId?: MobileNavigationItemId,
  ) => {
    if (!selectedGroup || configuredItems.has(itemId)) return
    const nextItems = [...selectedGroup.items]
    const beforeIndex = beforeId ? nextItems.indexOf(beforeId) : -1
    nextItems.splice(
      beforeIndex >= 0 ? beforeIndex : nextItems.length,
      0,
      itemId,
    )
    commit(normalized.groups.map(group => (
      group.id === selectedGroup.id
        ? { ...group, items: nextItems }
        : group
    )))
  }

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over) return
    const activeId = String(active.id)
    const overId = String(over.id)

    if (activeId.startsWith(GROUP_PALETTE_PREFIX)) {
      if (
        overId !== GROUP_DROP_ID
        && !overId.startsWith(GROUP_PREFIX)
      ) return
      const groupId = idAfterPrefix(
        activeId,
        GROUP_PALETTE_PREFIX,
      ) as MobileNavigationGroupId
      const beforeId = overId.startsWith(GROUP_PREFIX)
        ? idAfterPrefix(overId, GROUP_PREFIX) as MobileNavigationGroupId
        : undefined
      addGroup(groupId, beforeId)
      return
    }
    if (
      activeId.startsWith(GROUP_PREFIX)
      && overId.startsWith(GROUP_PREFIX)
    ) {
      const activeGroup = idAfterPrefix(
        activeId,
        GROUP_PREFIX,
      ) as MobileNavigationGroupId
      const overGroup = idAfterPrefix(
        overId,
        GROUP_PREFIX,
      ) as MobileNavigationGroupId
      const oldIndex = normalized.groups.findIndex(group => (
        group.id === activeGroup
      ))
      const newIndex = normalized.groups.findIndex(group => (
        group.id === overGroup
      ))
      if (oldIndex >= 0 && newIndex >= 0 && oldIndex !== newIndex) {
        commit(reorderMobileDockGroups(
          normalized,
          activeGroup,
          overGroup,
        ).groups)
      }
      return
    }
    if (activeId.startsWith(ITEM_PALETTE_PREFIX)) {
      if (
        overId !== ITEM_DROP_ID
        && !overId.startsWith(ITEM_PREFIX)
      ) return
      const itemId = idAfterPrefix(
        activeId,
        ITEM_PALETTE_PREFIX,
      ) as MobileNavigationItemId
      const beforeId = overId.startsWith(ITEM_PREFIX)
        ? idAfterPrefix(overId, ITEM_PREFIX) as MobileNavigationItemId
        : undefined
      addItem(itemId, beforeId)
      return
    }
    if (
      selectedGroup
      && activeId.startsWith(ITEM_PREFIX)
      && overId.startsWith(ITEM_PREFIX)
    ) {
      const activeItem = idAfterPrefix(
        activeId,
        ITEM_PREFIX,
      ) as MobileNavigationItemId
      const overItem = idAfterPrefix(
        overId,
        ITEM_PREFIX,
      ) as MobileNavigationItemId
      const oldIndex = selectedGroup.items.indexOf(activeItem)
      const newIndex = selectedGroup.items.indexOf(overItem)
      if (oldIndex >= 0 && newIndex >= 0 && oldIndex !== newIndex) {
        commit(reorderMobileDockItems(
          normalized,
          selectedGroup.id,
          activeItem,
          overItem,
        ).groups)
      }
    }
  }

  const moveGroup = (index: number, direction: -1 | 1) => {
    const target = index + direction
    if (target < 0 || target >= normalized.groups.length) return
    commit(reorderMobileDockGroups(
      normalized,
      normalized.groups[index].id,
      normalized.groups[target].id,
    ).groups)
  }

  const moveItem = (index: number, direction: -1 | 1) => {
    if (!selectedGroup) return
    const target = index + direction
    if (target < 0 || target >= selectedGroup.items.length) return
    commit(reorderMobileDockItems(
      normalized,
      selectedGroup.id,
      selectedGroup.items[index],
      selectedGroup.items[target],
    ).groups)
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
    >
      <div className="space-y-5">
        <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-medium text-slate-800">
                可用分类
              </div>
              <div className="mt-1 text-xs text-slate-500">
                拖入下方预览，最多 4 个分类
              </div>
            </div>
            <Tag>{normalized.groups.length} / {MAX_DOCK_GROUPS}</Tag>
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {availableGroups.length > 0 ? availableGroups.map(group => (
              <PaletteButton
                key={group.id}
                id={`${GROUP_PALETTE_PREFIX}${group.id}`}
                label={group.label}
                icon={<NavigationIcon name={group.icon} />}
                onAdd={() => addGroup(group.id)}
              />
            )) : (
              <div className="sm:col-span-3">
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="全部分类都已加入 Dock"
                />
              </div>
            )}
          </div>
        </div>

        <div className="dock-config-preview">
          <div className="dock-config-preview__screen">
            <div className="dock-config-preview__header">
              <span className="h-2.5 w-2.5 rounded-full bg-rose-300" />
              <span className="h-2.5 w-2.5 rounded-full bg-amber-300" />
              <span className="h-2.5 w-2.5 rounded-full bg-emerald-300" />
              <span className="ml-2 text-xs text-slate-400">
                手机 Dock 预览
              </span>
            </div>

            {selectedGroup && (
              <ItemDropZone>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-xs font-medium text-slate-600">
                    {selectedDefinition?.label}
                  </span>
                  <span className="text-[11px] text-slate-400">
                    拖动页面调整顺序
                  </span>
                </div>
                <SortableContext
                  items={selectedGroup.items.map(item => (
                    `${ITEM_PREFIX}${item}`
                  ))}
                  strategy={verticalListSortingStrategy}
                >
                  <div className="grid gap-2">
                    {selectedGroup.items.map((itemId, index) => (
                      <SortablePage
                        key={itemId}
                        itemId={itemId}
                        index={index}
                        total={selectedGroup.items.length}
                        onMove={direction => moveItem(index, direction)}
                        onRemove={() => {
                          if (selectedGroup.items.length <= 1) return
                          commit(normalized.groups.map(group => (
                            group.id === selectedGroup.id
                              ? {
                                  ...group,
                                  items: group.items.filter(id => id !== itemId),
                                }
                              : group
                          )))
                        }}
                      />
                    ))}
                  </div>
                </SortableContext>
              </ItemDropZone>
            )}

            <GroupDropZone>
              <SortableContext
                items={normalized.groups.map(group => (
                  `${GROUP_PREFIX}${group.id}`
                ))}
                strategy={horizontalListSortingStrategy}
              >
                <div className="dock-config-preview__groups">
                  {normalized.groups.map((group, index) => (
                    <SortableGroup
                      key={group.id}
                      group={group}
                      selected={selectedGroup?.id === group.id}
                      index={index}
                      total={normalized.groups.length}
                      onSelect={() => setSelectedGroupId(group.id)}
                      onMove={direction => moveGroup(index, direction)}
                      onRemove={() => {
                        if (normalized.groups.length <= 1) return
                        const next = normalized.groups.filter(current => (
                          current.id !== group.id
                        ))
                        commit(next)
                        if (selectedGroupId === group.id) {
                          setSelectedGroupId(next[0].id)
                        }
                      }}
                    />
                  ))}
                </div>
              </SortableContext>
            </GroupDropZone>
          </div>
        </div>

        {selectedGroup && (
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
            <div className="text-sm font-medium text-slate-800">
              可加入“{selectedDefinition?.label}”的页面
            </div>
            <div className="mt-1 text-xs text-slate-500">
              拖到预览中的圆角菜单，也可以点击加号
            </div>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {availableItems.length > 0 ? availableItems.map(item => (
                <PaletteButton
                  key={item.id}
                  id={`${ITEM_PALETTE_PREFIX}${item.id}`}
                  label={item.label}
                  icon={<NavigationIcon name={item.icon} />}
                  onAdd={() => addItem(item.id)}
                />
              )) : (
                <div className="sm:col-span-2">
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="该分类的页面都已加入"
                  />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </DndContext>
  )
}
