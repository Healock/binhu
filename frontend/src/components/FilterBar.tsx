import { useState } from 'react'
import type { Spreadsheet } from '../types'

interface Props {
  spreadsheets: Spreadsheet[]
  selectedId?: number
  inspectors: string[]
  onFilter: (params: {
    spreadsheet_id?: number
    inspector?: string
    start_date?: string
    end_date?: string
  }) => void
}

export default function FilterBar({ spreadsheets, selectedId, inspectors, onFilter }: Props) {
  const [spId, setSpId] = useState(selectedId?.toString() || '')
  const [inspector, setInspector] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  const handleSearch = () => {
    onFilter({
      spreadsheet_id: spId ? parseInt(spId) : undefined,
      inspector: inspector || undefined,
      start_date: startDate || undefined,
      end_date: endDate || undefined,
    })
  }

  const handleReset = () => {
    setSpId('')
    setInspector('')
    setStartDate('')
    setEndDate('')
    onFilter({})
  }

  return (
    <div className="app-card app-toolbar">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">表格</label>
          <select
            value={spId}
            onChange={(e) => setSpId(e.target.value)}
            className="border border-gray-300 rounded px-2 py-1.5 text-sm bg-white"
          >
            <option value="">全部表格</option>
            {spreadsheets.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">核查人</label>
          <select
            value={inspector}
            onChange={(e) => setInspector(e.target.value)}
            className="border border-gray-300 rounded px-2 py-1.5 text-sm bg-white"
          >
            <option value="">全部</option>
            {inspectors.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">开始日期</label>
          <input
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className="border border-gray-300 rounded px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">结束日期</label>
          <input
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            className="border border-gray-300 rounded px-2 py-1.5 text-sm"
          />
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleSearch}
            className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
          >
            查询
          </button>
          <button
            onClick={handleReset}
            className="px-4 py-1.5 border border-gray-300 rounded text-sm text-gray-600 hover:bg-gray-50"
          >
            重置
          </button>
        </div>
      </div>
    </div>
  )
}
