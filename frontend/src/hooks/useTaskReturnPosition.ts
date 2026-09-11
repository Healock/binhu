import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from 'react'

type Position = { key: string; offset: number; scrollTop: number; order: string[] }

/** Hold the task's viewport offset through asynchronous layout, until user scrolling. */
export function useTaskReturnPosition(root: RefObject<HTMLDivElement | null>, active: boolean, rowKeys: string[], initial?: { row_key: string; anchor_offset?: number; scroll_top: number } | null) {
  const saved = useRef<Position | null>(initial ? { key: initial.row_key, offset: initial.anchor_offset ?? 0, scrollTop: initial.scroll_top, order: rowKeys } : null)
  const [notice, setNotice] = useState('')
  const elements = useCallback(() => Array.from(root.current?.querySelectorAll<HTMLElement>('[data-mobile-task-row-key]') || [])
    .filter(element => element.getClientRects().length > 0), [root])
  const capture = useCallback((key: string) => {
    const container = root.current?.closest('main')
    const row = elements().find(element => element.dataset.mobileTaskRowKey === key)
    saved.current = {
      key, offset: row ? row.getBoundingClientRect().top - (container?.getBoundingClientRect().top || 0) : 0,
      scrollTop: container?.scrollTop ?? window.scrollY, order: rowKeys,
    }
    setNotice('')
  }, [elements, root, rowKeys])
  useLayoutEffect(() => {
    if (!active || !saved.current || !root.current) return
    const container = root.current.closest('main')
    let stopped = false
    const restore = () => {
      if (stopped || !saved.current) return
      const position = saved.current
      const available = elements()
      let anchor = available.find(element => element.dataset.mobileTaskRowKey === position.key)
      if (!anchor && available.length) {
        const index = position.order.indexOf(position.key)
        const remaining = new Map(available.map(element => [element.dataset.mobileTaskRowKey, element]))
        for (let distance = 1; distance <= position.order.length && !anchor; distance++) {
          anchor = remaining.get(position.order[index + distance]) || remaining.get(position.order[index - distance])
        }
        anchor ||= available[0]
        position.key = anchor.dataset.mobileTaskRowKey || ''
        setNotice('原任务已不在当前筛选中，已定位到附近任务。')
      }
      if (anchor) {
        const delta = anchor.getBoundingClientRect().top - (container?.getBoundingClientRect().top || 0) - position.offset
        if (Math.abs(delta) > 0.5) {
          if (container) container.scrollTop += delta
          else window.scrollBy(0, delta)
        }
      } else if (container) container.scrollTop = position.scrollTop
      else window.scrollTo(0, position.scrollTop)
    }
    const stop = () => { stopped = true; saved.current = null; observer.disconnect() }
    const keydown = (event: KeyboardEvent) => {
      if (['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(event.key)) stop()
    }
    const observer = new ResizeObserver(restore)
    observer.observe(root.current)
    if (container) observer.observe(container)
    restore()
    const surface = container || window
    surface.addEventListener('wheel', stop, { passive: true })
    surface.addEventListener('touchstart', stop, { passive: true })
    surface.addEventListener('pointerdown', stop, { passive: true })
    window.addEventListener('keydown', keydown)
    return () => {
      observer.disconnect()
      surface.removeEventListener('wheel', stop)
      surface.removeEventListener('touchstart', stop)
      surface.removeEventListener('pointerdown', stop)
      window.removeEventListener('keydown', keydown)
    }
  }, [active, elements, root, rowKeys])
  return { capture, notice }
}
