import { useEffect, useState, type RefObject } from 'react'

export type ResponsiveLayoutMode = 'compact' | 'standard' | 'wide'

export interface ResponsiveLayoutState {
  mode: ResponsiveLayoutMode
  width: number
  height: number
  isCompact: boolean
  isStandard: boolean
  isWide: boolean
  isShort: boolean
}

export function getResponsiveLayoutMode(width: number, height: number): ResponsiveLayoutMode {
  if (width < 1200 || height <= 760) return 'compact'
  if (width >= 1600) return 'wide'
  return 'standard'
}

function readViewportSize(container?: HTMLElement | null) {
  return {
    width: Math.round(container?.getBoundingClientRect().width || window.innerWidth),
    height: Math.round(window.innerHeight),
  }
}

export function useResponsiveLayout(
  containerRef?: RefObject<HTMLElement | null>,
): ResponsiveLayoutState {
  const [size, setSize] = useState(() => readViewportSize(containerRef?.current))

  useEffect(() => {
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const next = readViewportSize(containerRef?.current)
        setSize(previous => (
          previous.width === next.width && previous.height === next.height
            ? previous
            : next
        ))
      })
    }

    update()
    window.addEventListener('resize', update)
    const observer = containerRef?.current && typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(update)
      : undefined
    if (observer && containerRef?.current) observer.observe(containerRef.current)

    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', update)
      observer?.disconnect()
    }
  }, [containerRef])

  const mode = getResponsiveLayoutMode(size.width, size.height)
  return {
    mode,
    width: size.width,
    height: size.height,
    isCompact: mode === 'compact',
    isStandard: mode === 'standard',
    isWide: mode === 'wide',
    isShort: size.height <= 760,
  }
}
