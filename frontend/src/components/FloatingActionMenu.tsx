import { createContext, useContext, useLayoutEffect, useRef, useState, type CSSProperties, type PointerEvent, type PropsWithChildren } from 'react'
import { AppstoreOutlined, CloseOutlined } from '@ant-design/icons'
import { FloatButton } from 'antd'
import { useResponsiveLayout } from '../hooks/useResponsiveLayout'
import useMobileViewport from '../hooks/useMobileViewport'
import { clampFloatingActionPosition, type FloatingActionPosition } from '../utils/floatingActionPosition'

type FloatingActionMenuContextValue = { open: boolean }
const FloatingActionMenuContext = createContext<FloatingActionMenuContextValue>({ open: false })

export function useFloatingActionMenu(): FloatingActionMenuContextValue {
  return useContext(FloatingActionMenuContext)
}

export default function FloatingActionMenu({ children }: PropsWithChildren) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState<FloatingActionPosition | null>(null)
  const [dragging, setDragging] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  const gesture = useRef<{ id: number; x: number; y: number; origin: FloatingActionPosition; moved: boolean } | null>(null)
  const suppressClick = useRef(false)
  const { width, height } = useResponsiveLayout()
  const mobile = useMobileViewport()
  const bottomInset = () => (mobile ? 76 : 12) + Number.parseFloat(getComputedStyle(container.current!).paddingBottom || '0')

  useLayoutEffect(() => {
    const bounds = container.current!.getBoundingClientRect()
    setPosition(previous => clampFloatingActionPosition(previous ?? { x: bounds.x, y: bounds.y }, width, height, bottomInset()))
  }, [width, height, mobile])

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('.app-speed-dial__main')
    if (!button || !event.isPrimary || event.button !== 0 || gesture.current) return
    suppressClick.current = false
    const bounds = container.current!.getBoundingClientRect()
    gesture.current = { id: event.pointerId, x: event.clientX, y: event.clientY, origin: { x: bounds.x, y: bounds.y }, moved: false }
    button.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const active = gesture.current
    if (!active || active.id !== event.pointerId) return
    const dx = event.clientX - active.x
    const dy = event.clientY - active.y
    if (!active.moved && Math.hypot(dx, dy) < 6) return
    active.moved = true
    suppressClick.current = true
    setDragging(true)
    setPosition(clampFloatingActionPosition({ x: active.origin.x + dx, y: active.origin.y + dy }, width, height, bottomInset()))
  }
  const endGesture = (event: PointerEvent<HTMLDivElement>) => {
    if (gesture.current?.id !== event.pointerId) return
    gesture.current = null
    setDragging(false)
  }
  const style = position ? { left: position.x, top: position.y } : undefined
  return (
    <FloatingActionMenuContext.Provider value={{ open }}>
      <div
        ref={container}
        className={`app-speed-dial ${open ? 'is-open' : ''} ${dragging ? 'is-dragging' : ''} ${mobile ? 'is-mobile' : ''}`}
        style={{ ...style, '--action-x': position && position.x < width / 2 ? '64px' : '-64px', '--action-y': position && position.y < height / 2 ? '64px' : '-64px' } as CSSProperties}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endGesture}
        onPointerCancel={endGesture}
        onLostPointerCapture={endGesture}
      >
        {children}
        <FloatButton
          rootClassName="app-speed-dial__main"
          icon={open ? <CloseOutlined /> : <AppstoreOutlined />}
          aria-label={open ? '收起快捷功能' : '打开快捷功能'}
          onClick={event => {
            if (suppressClick.current && event.detail !== 0) {
              suppressClick.current = false
              return
            }
            setOpen(value => !value)
          }}
        />
      </div>
    </FloatingActionMenuContext.Provider>
  )
}
