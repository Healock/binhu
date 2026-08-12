import { useEffect, useRef, useState } from 'react'

export default function useDebouncedValue<T>(
  value: T,
  delay = 350,
  flushSignal: unknown = undefined,
): T {
  const [debounced, setDebounced] = useState(value)
  const previousFlushSignal = useRef(flushSignal)

  useEffect(() => {
    const shouldFlush = previousFlushSignal.current !== flushSignal
      || (typeof value === 'string' && value.length === 0)
    previousFlushSignal.current = flushSignal
    if (shouldFlush) {
      setDebounced(value)
      return undefined
    }
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [delay, flushSignal, value])

  return debounced
}
