import { useEffect, useMemo, useRef, useState } from 'react'
import { AnalyseCell, AnalyseRows, api, ChooseResult } from '../api'
import { FolderPicker } from './Dir'

// Same as the desktop Analyse tab: row N = the N-th newest (or oldest) image of
// every folder; one pick per row; Choose copies the picks + report.md/.csv.
const ZOOM_LEVELS = [100, 150, 220, 320, 480]
const PREVIEW_STEP = 80
const PREVIEW_GAP = 28

interface Props {
  outputDir: string
  folders: string[]
  setFolders: (folders: string[]) => void
  chosenDir: string
  setChosenDir: (dir: string) => void
}

function previewSize(imgW: number, imgH: number, pointerX: number): { size: number; right: boolean } {
  // web twin of image_generate.preview_geometry
  const sw = window.innerWidth, sh = window.innerHeight
  const right = sw - pointerX - PREVIEW_GAP - 8
  const left = pointerX - PREVIEW_GAP - 8
  const boxW = Math.min(sw * 0.75, Math.max(right, left))
  const boxH = sh * 0.85 - 80
  const scale = Math.min(boxW / imgW, boxH / imgH)
  if (scale >= 1) return { size: Math.max(imgW, imgH), right: right >= left }
  const longest = Math.max(imgW, imgH) * scale
  return { size: Math.max(2 * PREVIEW_STEP, Math.floor(longest / PREVIEW_STEP) * PREVIEW_STEP), right: right >= left }
}

export default function Analyse(p: Props) {
  const [newestFirst, setNewestFirst] = useState(true)
  const [data, setData] = useState<AnalyseRows | null>(null)
  const [picks, setPicks] = useState<Record<number, number>>({})
  const [zoom, setZoom] = useState(150)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [picker, setPicker] = useState<'add' | 'chosen' | null>(null)
  const [defaultChosen, setDefaultChosen] = useState('')
  const [result, setResult] = useState<ChooseResult | null>(null)
  const [preview, setPreview] = useState<{ path: string; caption: string; x: number; y: number; size: number; right: boolean } | null>(null)
  const hoverTimer = useRef<number | null>(null)

  useEffect(() => {
    api.defaultChosen(p.outputDir).then((r) => setDefaultChosen(r.chosen_dir)).catch(() => undefined)
  }, [p.outputDir])

  function pickPaths(rows = data?.rows ?? [], current = picks): Set<string> {
    return new Set(Object.entries(current).map(([r, c]) => rows[Number(r)]?.[c]?.path).filter(Boolean) as string[])
  }

  async function load(keep?: Set<string>, order = newestFirst, folders = p.folders) {
    setError('')
    if (!folders.length) { setData(null); setPicks({}); return }
    try {
      const next = await api.analyseRows(folders, order)
      const kept = keep ?? pickPaths()
      const nextPicks: Record<number, number> = {}
      let dropped = 0
      next.rows.forEach((row, r) => row.forEach((cell, c) => {
        if (cell && kept.has(cell.path)) {
          if (r in nextPicks) dropped += 1
          else nextPicks[r] = c
        }
      }))
      setData(next)
      setPicks(nextPicks)
      setNotice(dropped ? `${dropped} selection(s) dropped: the new order put two picks in the same row` : '')
    } catch (e) {
      setError((e as Error).message)
    }
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void load(undefined, newestFirst, p.folders) }, [p.folders.join('\n')])

  function toggle(r: number, c: number) {
    setPicks((old) => {
      const next = { ...old }
      if (next[r] === c) delete next[r]
      else next[r] = c  // only one per row: replaces the previous pick
      return next
    })
  }

  function invert() {
    const keep = pickPaths()
    setNewestFirst(!newestFirst)
    void load(keep, !newestFirst)
  }

  function removeFolder(i: number) {
    const removed = p.folders[i]
    const keep = new Set([...pickPaths()].filter((path) => !path.startsWith(removed.replace(/\/$/, '') + '/')))
    const next = p.folders.filter((_, j) => j !== i)
    p.setFolders(next)
    void load(keep, newestFirst, next)
  }

  function zoomStep(step: number) {
    const i = ZOOM_LEVELS.indexOf(zoom)
    setZoom(ZOOM_LEVELS[Math.max(0, Math.min(ZOOM_LEVELS.length - 1, i + step))])
  }

  function hover(cell: AnalyseCell, caption: string, e: React.MouseEvent<HTMLImageElement>) {
    const x = e.clientX, y = e.clientY
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = window.setTimeout(() => {
      const { size, right } = previewSize(cell.width || 1600, cell.height || 1000, x)
      setPreview({ path: cell.path, caption, x, y, size, right })
    }, 350)
  }

  function unhover() {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = null
    setPreview(null)
  }

  async function choose() {
    if (!data) return
    try {
      const res = await api.choose({
        folders: p.folders, picks: Object.fromEntries(Object.entries(picks).map(([r, c]) => [r, c])),
        chosen_dir: p.chosenDir, output_dir: p.outputDir, newest_first: newestFirst,
      })
      setResult(res)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const summary = useMemo(() => {
    if (!data) return 'Add at least one folder to start.'
    const selected = Object.keys(picks).length
    const parts = [`rows: ${data.rows.length}`, `selected: ${selected}`, `rows without a pick: ${data.rows.length - selected}`]
    data.folders.forEach((f, i) => {
      const chosen = Object.values(picks).filter((c) => c === i).length
      parts.push(`${i + 1}. ${f.name}: ${f.count} image(s), ${chosen} selected`)
    })
    return parts.join('  |  ')
  }, [data, picks])

  return (
    <div className="card analyse">
      <div className="analyse-bar">
        <button className="ghost" onClick={() => setPicker('add')}
          title="Add a folder of images to compare (one column per folder; at least 1, no limit).">Add folder…</button>
        <button className="ghost" onClick={invert}
          title="Invert the order: rows aligned from the newest image of each folder, or from the oldest.">
          Order: {newestFirst ? 'newest' : 'oldest'} first ⇅
        </button>
        <button className="ghost" onClick={() => setPicks({})}>Clear selection</button>
        <button className="ghost" onClick={() => void load()}>Refresh</button>
        <span className="analyse-zoom">
          <button className="ghost" onClick={() => zoomStep(-1)} disabled={zoom === ZOOM_LEVELS[0]}
            title="Smaller previews (down to 100 px), to see more rows at once.">Zoom −</button>
          <span className="hint">{zoom} px</span>
          <button className="ghost" onClick={() => zoomStep(1)} disabled={zoom === ZOOM_LEVELS[ZOOM_LEVELS.length - 1]}
            title="Bigger previews (up to 480 px). Selections are kept.">Zoom +</button>
        </span>
      </div>
      <div className="analyse-chips">
        {p.folders.map((f, i) => (
          <span key={f} className="chip" title={f}>
            {i + 1}. {f.split('/').filter(Boolean).pop()}
            <button className="chip-x" onClick={() => removeFolder(i)} aria-label={`remove ${f}`}>✕</button>
          </span>
        ))}
      </div>
      {error && <div className="notice">{error}</div>}
      {notice && <div className="hint">{notice}</div>}
      {data && (
        <div className="analyse-grid-wrap">
          <table className="analyse-grid">
            <thead>
              <tr>
                <th>row</th>
                {data.folders.map((f, i) => <th key={f.path}>{i + 1}. {f.name}<br /><span className="hint">{f.count} image(s)</span></th>)}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row, r) => (
                <tr key={r}>
                  <td className="analyse-row-no">{r + 1}</td>
                  {row.map((cell, c) => (
                    <td key={c}>
                      {cell ? (
                        <img src={api.thumbUrl(cell.path, zoom)} alt={cell.name}
                          className={`analyse-thumb${picks[r] === c ? ' picked' : ''}`}
                          style={{ maxWidth: zoom, maxHeight: zoom }}
                          onClick={() => toggle(r, c)}
                          onDoubleClick={() => window.open(api.fileUrl(cell.path), '_blank')}
                          onMouseEnter={(e) => hover(cell, `${cell.name}  ·  ${cell.path.slice(0, cell.path.lastIndexOf('/'))}\nmodified ${cell.modified} · ${Math.round(cell.size / 1024)} KB  ·  click = select, double-click = open full size`, e)}
                          onMouseLeave={unhover} onMouseDown={unhover} onWheel={unhover} />
                      ) : <span className="analyse-empty" style={{ width: zoom, height: zoom * 0.66 }}>—</span>}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="analyse-summary">{summary}</div>
      <div className="pick-row analyse-choose">
        <span className="hint" style={{ alignSelf: 'center' }}>Chosen dir:</span>
        <input type="text" value={p.chosenDir} onChange={(e) => p.setChosenDir(e.target.value)}
          placeholder={defaultChosen ? `(empty = ${defaultChosen})` : 'empty = <parent of Output dir>/chosen'}
          title="Where Choose puts its copies: a new <chosen dir>/<date_time>/ folder with the images + report.md and report.csv." />
        <button className="ghost" onClick={() => setPicker('chosen')}>Browse…</button>
        <button className="btn-generate" onClick={() => void choose()} disabled={!Object.keys(picks).length}
          title="Copy the selected images (one per row) and write a report with where each one came from, its prompt/model/seed and what it was chosen over. Originals are never moved or changed.">
          Choose
        </button>
      </div>
      {preview && (
        <div className="analyse-preview" style={{
          top: Math.max(8, Math.min(preview.y - preview.size / 3, window.innerHeight - preview.size - 90)),
          ...(preview.right ? { left: preview.x + PREVIEW_GAP } : { right: window.innerWidth - preview.x + PREVIEW_GAP }),
        }}>
          <img src={api.thumbUrl(preview.path, preview.size)} alt="" style={{ maxWidth: preview.size, maxHeight: preview.size }} />
          <div className="analyse-preview-caption">{preview.caption}</div>
        </div>
      )}
      {picker && (
        <FolderPicker
          initial={picker === 'add' ? (p.folders[p.folders.length - 1] ?? p.outputDir) : (p.chosenDir || defaultChosen)}
          title={picker === 'add' ? 'folder to compare' : 'chosen dir'}
          onPick={(dir) => {
            setPicker(null)
            if (picker === 'chosen') { p.setChosenDir(dir); return }
            if (p.folders.includes(dir)) { setNotice('This folder is already in the table.'); return }
            p.setFolders([...p.folders, dir])
          }}
          onClose={() => setPicker(null)} />
      )}
      {result && (
        <div className="modal-bg" onClick={() => setResult(null)}>
          <div className="modal success-modal" onClick={(e) => e.stopPropagation()}>
            <div className="success-head">
              <span className="success-badge" aria-hidden="true">✓</span>
              <div>
                <h3>Copied {result.rows.length} image(s)</h3>
                <div className="hint">with report.md and report.csv</div>
              </div>
            </div>
            <code className="path-text" style={{ display: 'block', margin: '10px 0' }}>{result.folder}</code>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              <a href={api.fileUrl(result.report_md)} target="_blank" rel="noreferrer"><button className="ghost">report.md</button></a>
              <a href={api.fileUrl(result.report_csv)} target="_blank" rel="noreferrer"><button className="ghost">report.csv</button></a>
              <button className="ghost" onClick={() => void api.open(result.folder)}>Open folder</button>
              <button className="primary" onClick={() => setResult(null)}>OK</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
