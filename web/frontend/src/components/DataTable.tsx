import { ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { columnKind, Filters, matchesFilters, SortState, sortRows } from '../tableView'

export interface Column { key: string; label: string; width?: number }
export interface Row<P = unknown> {
  id: string
  cells: Record<string, string>          // what is shown / filtered
  sort?: Record<string, string>          // optional sort text (e.g. full date)
  className?: string
  payload: P
}

interface Props<P> {
  columns: Column[]
  rows: Row<P>[]
  sort: SortState | null
  onSortChange: (sort: SortState | null) => void
  onRowClick?: (row: Row<P>) => void
  onRowDoubleClick?: (row: Row<P>) => void
  onRowContextMenu?: (row: Row<P>, x: number, y: number) => void
  footer?: (visible: Row<P>[], total: number, filtered: boolean) => ReactNode
  maxHeight?: number
  rowTitle?: string
}

// Sortable (click a heading) + spreadsheet-like filter (right-click a heading)
// table - the web twin of image_generate.TreeTable.
export default function DataTable<P>(p: Props<P>) {
  const [filters, setFilters] = useState<Filters>({})
  const [popup, setPopup] = useState<{ col: string; x: number; y: number } | null>(null)
  const get = (row: Row<P>, col: string) => (row.sort?.[col] ?? row.cells[col] ?? '')
  const visible = useMemo(
    () => sortRows(p.rows.filter((r) => matchesFilters(r.cells, filters)), p.sort, get),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [p.rows, filters, p.sort])
  const filtered = Object.keys(filters).length > 0

  function toggleSort(col: string) {
    if (p.sort?.col === col) p.onSortChange({ col, desc: !p.sort.desc })
    else p.onSortChange({ col, desc: false })
  }

  return (
    <div>
      <div className="dt-wrap" style={{ maxHeight: p.maxHeight ?? 380 }}>
        <table className="dt">
          <thead>
            <tr>
              {p.columns.map((c) => (
                <th key={c.key} style={{ minWidth: c.width }}
                  title="Click: sort · right-click: filter"
                  onClick={() => toggleSort(c.key)}
                  onContextMenu={(e) => { e.preventDefault(); setPopup({ col: c.key, x: e.clientX, y: e.clientY }) }}>
                  {c.label}
                  {p.sort?.col === c.key && (p.sort.desc ? ' ▼' : ' ▲')}
                  {filters[c.key] && ' ▾'}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
              <tr key={row.id} className={row.className} title={p.rowTitle}
                onClick={() => p.onRowClick?.(row)}
                onDoubleClick={() => p.onRowDoubleClick?.(row)}
                onContextMenu={(e) => {
                  if (!p.onRowContextMenu) return
                  e.preventDefault(); p.onRowContextMenu(row, e.clientX, e.clientY)
                }}>
                {p.columns.map((c) => {
                  const text = row.cells[c.key] ?? ''
                  return <td key={c.key} title={text.length > 30 ? text : undefined}>{text}</td>
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="dt-footer">
        <span className="hint dt-footer-text">{p.footer?.(visible, p.rows.length, filtered)}</span>
        <button className="ghost dt-clear" onClick={() => setFilters({})} disabled={!filtered}
          title="Show all rows again. Click a column heading to sort, right-click it to filter.">
          Clear filters
        </button>
      </div>
      {popup && (
        <FilterPopup
          col={popup.col} x={popup.x} y={popup.y}
          label={p.columns.find((c) => c.key === popup.col)?.label ?? popup.col}
          values={p.rows.map((r) => r.cells[popup.col] ?? '')}
          current={filters[popup.col]}
          onSort={(desc) => { setPopup(null); p.onSortChange({ col: popup.col, desc }) }}
          onApply={(rule) => {
            setPopup(null)
            setFilters((old) => {
              const next = { ...old }
              if (rule) next[popup.col] = rule
              else delete next[popup.col]
              return next
            })
          }}
          onClose={() => setPopup(null)} />
      )}
    </div>
  )
}

const MAX_VALUES = 300

function FilterPopup({ label, values, current, x, y, onSort, onApply, onClose }: {
  col: string; label: string; values: string[]; x: number; y: number
  current?: { values: Set<string> | null; contains: string }
  onSort: (desc: boolean) => void
  onApply: (rule: { values: Set<string> | null; contains: string } | null) => void
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const counts = useMemo(() => {
    const m = new Map<string, number>()
    values.forEach((v) => m.set(v, (m.get(v) ?? 0) + 1))
    return m
  }, [values])
  const kind = columnKind([...counts.keys()])
  const distinct = useMemo(() => sortRows([...counts.keys()], { col: 'v', desc: false }, (v) => v), [counts])
  const listed = distinct.slice(0, MAX_VALUES)
  const [checked, setChecked] = useState<Set<string>>(
    () => new Set(current?.values ? [...current.values] : listed))
  const [contains, setContains] = useState(current?.contains ?? '')
  useEffect(() => {
    const down = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) onClose() }
    window.addEventListener('mousedown', down)
    return () => window.removeEventListener('mousedown', down)
  }, [onClose])
  const [low, high] = kind === 'num' ? ['1 → 9', '9 → 1'] : kind === 'date' ? ['old → new', 'new → old'] : ['A → Z', 'Z → A']

  function apply() {
    const everything = checked.size === listed.length && distinct.length <= MAX_VALUES
    if (everything && !contains.trim()) onApply(null)
    else onApply({ values: everything ? null : new Set(checked), contains: contains.trim() })
  }

  return (
    <div ref={ref} className="dt-pop"
      style={{ left: Math.min(x, window.innerWidth - 340), top: Math.min(y + 8, window.innerHeight - 420) }}
      onKeyDown={(e) => { if (e.key === 'Escape') onClose(); if (e.key === 'Enter') apply() }}>
      <div className="dt-pop-title">{label}</div>
      <div className="dt-pop-row">
        <span className="hint">Sort:</span>
        <button className="ghost" onClick={() => onSort(false)}>▲ {low}</button>
        <button className="ghost" onClick={() => onSort(true)}>▼ {high}</button>
      </div>
      <input type="text" placeholder="Contains…" value={contains} autoFocus
        onChange={(e) => setContains(e.target.value)} />
      <div className="dt-pop-row">
        <button className="ghost" onClick={() => setChecked(new Set(listed))}>All</button>
        <button className="ghost" onClick={() => setChecked(new Set())}>None</button>
      </div>
      <div className="dt-pop-list">
        {listed.map((v) => (
          <label key={v} className="dt-pop-item">
            <input type="checkbox" checked={checked.has(v)} onChange={(e) => {
              const next = new Set(checked)
              if (e.target.checked) next.add(v); else next.delete(v)
              setChecked(next)
            }} />
            <span>{(v.trim() ? v : '(empty)').slice(0, 60)}</span>
            <span className="hint">({counts.get(v)})</span>
          </label>
        ))}
        {distinct.length > MAX_VALUES && (
          <div className="hint">{distinct.length - MAX_VALUES} more values: use Contains</div>
        )}
      </div>
      <div className="dt-pop-row" style={{ justifyContent: 'space-between' }}>
        <button className="ghost" onClick={() => onApply(null)}>Clear filter</button>
        <span>
          <button className="ghost" onClick={onClose}>Cancel</button>{' '}
          <button className="primary" onClick={apply}>OK</button>
        </span>
      </div>
    </div>
  )
}
