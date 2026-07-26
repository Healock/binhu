import { useState, useCallback, useEffect } from 'react'
import type { StatsItem, StatsResponse } from '../types'
import { queryStats, getInspectors, getDateRange } from '../api/client'

export function useStats(spreadsheetId?: number) {
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [inspectors, setInspectors] = useState<string[]>([])
  const [dateRange, setDateRange] = useState<{ min_date: string | null; max_date: string | null }>({ min_date: null, max_date: null })

  const fetchStats = useCallback(async (params: {
    inspector?: string
    start_date?: string
    end_date?: string
    page?: number
    page_size?: number
  } = {}) => {
    setLoading(true)
    try {
      const data = await queryStats({ ...params, spreadsheet_id: spreadsheetId })
      setStats(data)
    } finally {
      setLoading(false)
    }
  }, [spreadsheetId])

  const fetchFilters = useCallback(async () => {
    try {
      const [insp, dr] = await Promise.all([
        getInspectors(spreadsheetId),
        getDateRange(spreadsheetId),
      ])
      setInspectors(insp)
      setDateRange(dr)
    } catch (e) {
      // ignore
    }
  }, [spreadsheetId])

  useEffect(() => {
    fetchFilters()
    fetchStats()
  }, [fetchFilters, fetchStats])

  return { stats, loading, inspectors, dateRange, fetchStats, fetchFilters }
}
