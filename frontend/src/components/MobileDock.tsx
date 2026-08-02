import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import type { MobileDockConfig, PermissionCode, Role } from '../types'
import {
  navigationGroupById,
  navigationItemById,
  normalizeMobileDockConfig,
  routeIsActive,
  mobileNavigationItemLabel,
} from '../navigation/mobileNavigation'
import NavigationIcon from './NavigationIcon'
import { confirmPendingNavigation } from '../utils/navigationGuard'

function useVirtualKeyboardOpen() {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const viewport = window.visualViewport
    let largestViewportHeight = viewport?.height || window.innerHeight

    const update = () => {
      const currentViewportHeight = viewport?.height || window.innerHeight
      largestViewportHeight = Math.max(
        largestViewportHeight,
        currentViewportHeight,
      )
      const heightLoss = viewport
        ? window.innerHeight - viewport.height
        : 0
      const viewportLoss = largestViewportHeight - currentViewportHeight
      const activeElement = document.activeElement
      const editing = (
        activeElement instanceof HTMLInputElement
        || activeElement instanceof HTMLTextAreaElement
        || activeElement instanceof HTMLSelectElement
        || activeElement?.getAttribute('contenteditable') === 'true'
      )
      setOpen(
        heightLoss > Math.max(180, window.innerHeight * 0.22)
        || (editing && viewportLoss > 120),
      )
    }
    update()
    viewport?.addEventListener('resize', update)
    window.addEventListener('resize', update)
    document.addEventListener('focusin', update)
    document.addEventListener('focusout', update)
    return () => {
      viewport?.removeEventListener('resize', update)
      window.removeEventListener('resize', update)
      document.removeEventListener('focusin', update)
      document.removeEventListener('focusout', update)
    }
  }, [])

  return open
}

export default function MobileDock({
  config,
  role,
  permissions,
  position,
}: {
  config: MobileDockConfig
  role: Role
  permissions: PermissionCode[]
  position?: string | null
}) {
  const navigate = useNavigate()
  const location = useLocation()
  const rootRef = useRef<HTMLDivElement>(null)
  const [openGroupId, setOpenGroupId] = useState<string | null>(null)
  const keyboardOpen = useVirtualKeyboardOpen()
  const normalized = useMemo(
    () => normalizeMobileDockConfig(config, role, permissions),
    [config, permissions, role],
  )
  const groups = normalized.groups.flatMap((groupConfig) => {
    const definition = navigationGroupById(groupConfig.id)
    if (!definition) return []
    const items = groupConfig.items.flatMap((itemId) => {
      const item = navigationItemById(itemId)
      return item ? [item] : []
    })
    return items.length > 0
      ? [{ config: groupConfig, definition, items }]
      : []
  })
  const openGroup = groups.find(group => (
    group.definition.id === openGroupId
  ))

  useEffect(() => {
    setOpenGroupId(null)
  }, [location.pathname])

  useEffect(() => {
    const closeOnOutside = (event: PointerEvent) => {
      if (
        rootRef.current
        && !rootRef.current.contains(event.target as Node)
      ) {
        setOpenGroupId(null)
      }
    }
    document.addEventListener('pointerdown', closeOnOutside)
    return () => document.removeEventListener('pointerdown', closeOnOutside)
  }, [])

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenGroupId(null)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [])

  if (keyboardOpen || groups.length === 0) return null

  return (
    <div
      ref={rootRef}
      className="mobile-dock-root md:hidden"
      aria-label="手机快捷导航"
    >
      {openGroup && (
        <div
          className="mobile-dock-menu"
          role="menu"
          aria-label={`${openGroup.definition.label}页面`}
        >
          <div className="mobile-dock-menu__title">
            {openGroup.definition.label}
          </div>
          <div
            className="mobile-dock-menu__items"
            style={{
              gridTemplateColumns: `repeat(${Math.min(openGroup.items.length, 3)}, minmax(0, 1fr))`,
            }}
          >
            {openGroup.items.map((item) => {
              const active = routeIsActive(location.pathname, item)
              return (
                <button
                  key={item.id}
                  type="button"
                  role="menuitem"
                  className={`mobile-dock-menu__item${active ? ' is-active' : ''}`}
                  onClick={() => {
                    if (!confirmPendingNavigation()) return
                    setOpenGroupId(null)
                    navigate(item.path)
                  }}
                >
                  <NavigationIcon name={item.icon} />
                  <span>{mobileNavigationItemLabel(item, position, true)}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div className="mobile-dock-bar">
        <div className="mobile-dock-bar__items">
          {groups.map(({ definition, items }) => {
            const active = items.some(item => (
              routeIsActive(location.pathname, item)
            ))
            const expanded = openGroupId === definition.id
            return (
              <button
                key={definition.id}
                type="button"
                className={`mobile-dock-bar__item${active ? ' is-active' : ''}`}
                aria-expanded={expanded}
                aria-haspopup="menu"
                onClick={() => setOpenGroupId(current => (
                  current === definition.id ? null : definition.id
                ))}
              >
                <span className="mobile-dock-bar__icon">
                  <NavigationIcon name={definition.icon} />
                </span>
                <span>{definition.dockLabel}</span>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
