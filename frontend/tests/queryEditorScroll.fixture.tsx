import { createRoot } from 'react-dom/client'
import { QuerySpreadsheet } from '../src/components/QuerySpreadsheet'
import type { QueryDataRow } from '../src/api/client'
import type { QueryDisplayRow } from '../src/utils/queryGrid'
import type { QuerySheetCellChange } from '../src/utils/querySpreadsheet'
import '../src/index.css'

const columns = Array.from({ length: 14 }, (_, index) => `列${index + 1}`)
const rows: QueryDataRow[] = Array.from({ length: 80 }, (_, row) => {
  const data: QueryDataRow = {
    __row_key: `fixture:${row}`,
    __source_id: row + 1,
    __revision: 1,
    __editable_fields: columns,
  }
  columns.forEach((column, columnIndex) => { data[column] = `虚构-${row + 1}-${columnIndex + 1}` })
  return data
})

function Fixture() {
  return <main style={{ height: '100vh', overflow: 'auto', padding: 24 }}>
    <QuerySpreadsheet
      businessType="全链条"
      source="online"
      rows={rows}
      columns={columns}
      columnMeta={columns.map(field => ({ field, type: 'text' as const }))}
      drafts={[] as QueryDisplayRow[]}
      canAdd={false}
      revision={1}
      filterCriteria={{}}
      onSortChange={() => {}}
      onDraftsChange={() => {}}
      onFilterCriteriaChange={() => {}}
      onSelectionChange={() => {}}
      onCommit={async (_changes: QuerySheetCellChange[]) => {}}
      onBlocked={() => {}}
    />
  </main>
}

createRoot(document.getElementById('root')!).render(<Fixture />)
