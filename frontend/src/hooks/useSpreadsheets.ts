import { useState, useCallback, useEffect } from 'react'
import type { Spreadsheet } from '../types'
import { listSpreadsheets, createSpreadsheet, deleteSpreadsheet } from '../api/client'

export function useSpreadsheets() {
  const [spreadsheets, setSpreadsheets] = useState<Spreadsheet[]>([])
  const [loading, setLoading] = useState(false)

  const fetch = useCallback(async () => {
    setLoading(true)
    try {
      const data = await listSpreadsheets()
      setSpreadsheets(data)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetch() }, [fetch])

  const add = useCallback(async (payload: Parameters<typeof createSpreadsheet>[0]) => {
    await createSpreadsheet(payload)
    await fetch()
  }, [fetch])

  const remove = useCallback(async (id: number) => {
    await deleteSpreadsheet(id)
    await fetch()
  }, [fetch])

  return { spreadsheets, loading, fetch, add, remove }
}
