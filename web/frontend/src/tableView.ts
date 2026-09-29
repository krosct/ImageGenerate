// Same rules as image_generate.py (column_kind / sort_key / apply_table_view):
// numbers and ISO dates sort by value, text case-insensitively, empties last.
export type Kind = 'num' | 'date' | 'text'
export interface ColumnFilter { values: Set<string> | null; contains: string }
export type Filters = Record<string, ColumnFilter>
export interface SortState { col: string; desc: boolean }

const DATE_RE = /^\d{4}-\d{2}-\d{2}/

function asNumber(text: string): number {
  return Number(text.replace(/[$,]/g, ''))
}

export function columnKind(values: string[]): Kind {
  const present = values.map((v) => v.trim()).filter(Boolean)
  if (!present.length) return 'text'
  if (present.every((v) => !Number.isNaN(asNumber(v)) && v !== '')) return 'num'
  if (present.every((v) => DATE_RE.test(v))) return 'date'
  return 'text'
}

export function compareCells(a: string, b: string, kind: Kind): number {
  if (kind === 'num') return asNumber(a) - asNumber(b)
  const x = a.toLocaleLowerCase(), y = b.toLocaleLowerCase()
  return x < y ? -1 : x > y ? 1 : 0
}

export function matchesFilters(cells: Record<string, string>, filters: Filters): boolean {
  for (const [col, rule] of Object.entries(filters)) {
    const cell = cells[col] ?? ''
    if (rule.values && !rule.values.has(cell)) return false
    const needle = rule.contains.trim().toLocaleLowerCase()
    if (needle && !cell.toLocaleLowerCase().includes(needle)) return false
  }
  return true
}

export function sortRows<T>(rows: T[], sort: SortState | null, get: (row: T, col: string) => string): T[] {
  if (!sort) return rows
  const kind = columnKind(rows.map((r) => get(r, sort.col)))
  const filled = rows.filter((r) => get(r, sort.col).trim())
  const empty = rows.filter((r) => !get(r, sort.col).trim())
  // stable sort (Array.prototype.sort is stable in modern browsers)
  filled.sort((a, b) => compareCells(get(a, sort.col), get(b, sort.col), kind) * (sort.desc ? -1 : 1))
  return [...filled, ...empty]
}

export function parseSort(text: string | undefined): SortState | null {
  const [col, dir] = (text ?? '').split(':')
  if (!col) return null
  return { col, desc: dir === 'desc' }
}

export function formatSort(sort: SortState | null): string {
  return sort ? `${sort.col}:${sort.desc ? 'desc' : 'asc'}` : ''
}
