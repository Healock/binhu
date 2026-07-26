import type { StatsItem } from '../types'

function fmtRate(v: number): string {
  return (v * 100).toFixed(1) + '%'
}

function rateColor(v: number): string {
  if (v >= 0.8) return 'text-green-600 font-medium'
  if (v >= 0.5) return 'text-yellow-600 font-medium'
  return 'text-red-600 font-medium'
}

interface Props {
  data: StatsItem[]
  loading: boolean
  page: number
  total: number
  pageSize: number
  onPageChange: (page: number) => void
}

export default function PivotTable({ data, loading, page, total, pageSize, onPageChange }: Props) {
  const totalPages = Math.ceil(total / pageSize)

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400">
        加载中...
      </div>
    )
  }

  if (!data.length) {
    return (
      <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400">
        暂无数据，请先同步数据
      </div>
    )
  }

  const headers = [
    '核查人', '下发日期', '数据总数', '已核查', '未核查',
    '核查完成率', '无法核实', '移交', '已登记', '通勤',
    '离苏', '无法见底数', '核查见底率', '更新时间',
  ]

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50 sticky top-0 z-10">
            <tr>
              {headers.map((h, i) => (
                <th key={i} className="px-3 py-2.5 text-left font-medium text-gray-600 whitespace-nowrap">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.map((row, idx) => (
              <tr key={idx} className={idx % 2 === 0 ? 'bg-white' : 'bg-gray-50/50'}>
                <td className="px-3 py-2 whitespace-nowrap text-gray-800">{row.核查人}</td>
                <td className="px-3 py-2 whitespace-nowrap text-gray-600">{row.下发日期}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center">{row.数据总数}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center text-green-600">{row.已核查}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center text-red-500">{row.未核查}</td>
                <td className={`px-3 py-2 whitespace-nowrap text-center ${rateColor(row.核查完成率)}`}>
                  {fmtRate(row.核查完成率)}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-center">{row.无法核实}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center">{row.移交}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center">{row.已登记}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center">{row.通勤}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center">{row.离苏}</td>
                <td className="px-3 py-2 whitespace-nowrap text-center font-medium text-orange-600">{row.无法见底数}</td>
                <td className={`px-3 py-2 whitespace-nowrap text-center ${rateColor(row.核查见底率)}`}>
                  {fmtRate(row.核查见底率)}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-gray-400 text-xs">{row.computed_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div className="flex items-center justify-between px-4 py-3 border-t border-gray-200">
          <span className="text-sm text-gray-500">
            共 {total} 条记录，第 {page}/{totalPages} 页
          </span>
          <div className="flex space-x-1">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              className="px-3 py-1 text-sm rounded border border-gray-300 disabled:opacity-30 disabled:cursor-not-allowed hover:bg-gray-50"
            >
              上一页
            </button>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages}
              className="px-3 py-1 text-sm rounded border border-gray-300 disabled:opacity-30 disabled:cursor-not-allowed hover:bg-gray-50"
            >
              下一页
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
