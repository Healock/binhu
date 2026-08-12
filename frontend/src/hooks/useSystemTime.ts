import { useCallback } from 'react'
import { formatUTCTime } from '../api/client'
import { useAuth } from '../context/AuthContext'

export default function useSystemTime() {
  const { systemTimezone } = useAuth()

  return useCallback(
    (value: string | null | undefined) => formatUTCTime(value, systemTimezone),
    [systemTimezone],
  )
}
