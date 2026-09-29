import { useEffect, useRef, useState } from 'react'
import { api, copyText, listenStoryJob, StoryConfig, StoryLog, StoryMeta } from '../api'
import DataTable, { Row } from '../components/DataTable'
import ContextMenu, { MenuItem } from '../components/ContextMenu'
import { FolderPicker } from '../tabs/Dir'
import { formatSort, parseSort } from '../tableView'

// Web twin of the desktop StoryGenerate "Generate" tab.
interface Props {
  cfg: StoryConfig
  meta: StoryMeta
  set: <K extends keyof StoryConfig>(key: K, value: StoryConfig[K]) => void
  apiKey: string
  rememberKey: boolean
  openInPlayer: (folder: string) => void
  goVoices: () => void
  refreshKey: number
}

const COLUMNS = [
  { key: 'status', label: 'status' }, { key: 'date', label: 'date' },
  { key: 'title', label: 'title / storyboard' }, { key: 'storyboard_dir', label: 'storyboards dir' },
  { key: 'style', label: 'writer' }, { key: 'scenes', label: 'scenes' },
  { key: 'voice_label', label: 'voice' }, { key: 'audio_seconds', label: 'audio s' },
  { key: 'target_seconds', label: 'target s' }, { key: 'writer_cost_usd', label: 'writer $' },
  { key: 'tts_cost_usd', label: 'narration $' }, { key: 'detail', label: 'folder / error' },
]
const STATUS_LABEL: Record<string, string> = { error: '✖ error', deleted: '🗑 deleted', 'no audio': '🔇 no audio' }

function parent(path: string): string { return path.slice(0, Math.max(0, path.lastIndexOf('/'))) }
function base(path: string): string { return path.slice(path.lastIndexOf('/') + 1) }

function rowCost(r: Record<string, string>): number {
  return (Number(r.writer_cost_usd) || 0) + (Number(r.tts_cost_usd) || 0)
}

export default function StoryGenerateTab(p: Props) {
  const [log, setLog] = useState<StoryLog | null>(null)
  const [running, setRunning] = useState(false)
  const [jobId, setJobId] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [progress, setProgress] = useState('')
  const [step, setStep] = useState('')
  const [stepSeconds, setStepSeconds] = useState(0)
  const [status, setStatus] = useState('idle')
  const [picker, setPicker] = useState<'input_dir' | 'output_dir' | null>(null)
  const [confirm, setConfirm] = useState<{ text: string; run: () => void } | null>(null)
  const [message, setMessage] = useState<{ title: string; text: string; actions?: MenuItem[] } | null>(null)
  const [menu, setMenu] = useState<{ x: number; y: number; row: Record<string, string> } | null>(null)
  const stopListening = useRef<(() => void) | null>(null)

  async function refreshLog() {
    try {
      setLog(await api.storyLog(p.cfg.output_dir, p.cfg.style))
    } catch (e) {
      setStatus(`log error: ${(e as Error).message}`)
    }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void refreshLog() }, [p.cfg.output_dir, p.cfg.style, p.refreshKey])
  useEffect(() => () => stopListening.current?.(), [])

  function dirLabel(source: string): string {
    if (!source) return ''
    const folder = parent(source)
    let where = parent(folder)
    const home = log?.home ?? ''
    if (home && (where === home || where.startsWith(home + '/'))) where = '~' + where.slice(home.length)
    return `${base(folder)}  (${where})`
  }

  function rowDetails(row: Record<string, string>): string {
    const style = row.style || 'descriptive'
    const when = (row.date || '').replace('T', ' ').slice(0, 19)
    const pairs: [string, string][] = row.status === 'error' ? [
      ['Status', 'error'], ['Error', row.error || '(no detail)'], ['Storyboard', row.source_image || ''],
      ['Storyboards dir', row.source_image ? parent(row.source_image) : ''], ['When', when],
      ['Writer', `${row.writer_model} (${style})`], ['Narrator', row.tts_model || ''],
      ['Target', row.target_seconds ? `${row.target_seconds} s` : 'automatic'],
    ] : [
      ['Status', row.status || 'ok'], ['Title', row.title || ''],
      ['Folder', `${log?.output_dir ?? ''}/${row.folder || ''}`], ['Storyboard', row.source_image || ''],
      ['Storyboards dir', row.source_image ? parent(row.source_image) : ''], ['When', when],
      ['Writer', `${row.writer_model} (${style}${row.writer_effort ? `, thinking ${row.writer_effort}` : ''})`],
      ['Narration', `${row.voice_label || row.voice_id || '-'} via ${row.tts_model} - ${row.audio_seconds} s`
        + (row.target_seconds ? ` (target ${row.target_seconds} s)` : '') + `, ${row.scenes} scenes`],
      ['Cost', `writer $${row.writer_cost_usd || '0'} + narration $${row.tts_cost_usd || '(pending)'}`],
      ['Took', `${row.total_seconds} s`],
    ]
    return pairs.map(([k, v]) => `${k}: ${v}`).join('\n')
  }

  function start(body: Record<string, unknown>, what: string) {
    const voices = p.cfg.voices.filter((v) => v.id.trim())
    if (!p.cfg.dry_run && !voices.length) {
      setMessage({ title: 'Voices', text: 'No voice configured: add 1 to 5 Fish Audio voice ids (Voices tab).' })
      p.goVoices()
      return
    }
    const style = (body.style as string) || p.cfg.style
    const styleLabel = p.meta.styles.find((s) => s.id === style)?.label ?? style
    const run = async () => {
      setConfirm(null)
      setRunning(true); setElapsed(0); setStep('starting'); setStepSeconds(0)
      setStatus('')
      try {
        if (p.rememberKey && p.apiKey.trim() && !p.cfg.dry_run) await api.rememberKey('openrouter', p.apiKey.trim())
        const res = await api.storyGenerate({
          output_dir: p.cfg.output_dir, writer_model: p.cfg.writer_model, tts_model: p.cfg.tts_model,
          language: p.cfg.language, style, duration: p.cfg.duration, voices,
          dry_run: p.cfg.dry_run, force: p.cfg.force, api_key: p.apiKey || null, ...body,
        })
        setJobId(res.job_id)
        setProgress(`generating... 0/${res.total}`)
        stopListening.current = listenStoryJob(res.job_id, (ev) => {
          if (ev.elapsed !== undefined) setElapsed(ev.elapsed)
          if (ev.status === 'running') {
            if (ev.progress) {
              const failed = ev.progress.failed ? ` (${ev.progress.failed} failed)` : ''
              setProgress(`generating... ${ev.progress.done}/${ev.progress.total} done${failed}`)
            }
            if (ev.step) { setStep(ev.step); setStepSeconds(ev.step_seconds ?? 0) }
            void refreshLog()
            return
          }
          setRunning(false); setJobId(null); setStep('')
          void refreshLog()
          if (ev.status === 'cancelled') { setStatus('cancelled: unfinished story removed, nothing logged'); return }
          if (ev.status === 'error') {
            setStatus(`error: ${ev.error}`)
            setMessage({ title: 'Story generation failed', text: ev.error ?? '' })
            return
          }
          const r = ev.result!
          setElapsed(r.elapsed)
          setStatus(`finished: ${r.created.length} created, ${r.skipped.length} skipped, ${r.errors.length} failed | $${r.cost.toFixed(6)} (writer + narration)`)
          if (r.errors.length) {
            setMessage({
              title: 'Some storyboards failed',
              text: r.errors.slice(0, 5).map((e) => `${base(e.source)}: ${e.error.slice(0, 300)}`).join('\n\n')
                + (r.errors.length > 5 ? '\n\n…' : '')
                + "\n\nThey are the red rows in the log. To try again: click 'Retry failed' (only these), or double-click / right-click a red row.",
            })
          } else if (r.created.length) {
            p.openInPlayer(r.created[r.created.length - 1])
          }
        })
      } catch (e) {
        setRunning(false)
        setStatus(`error: ${(e as Error).message}`)
      }
    }
    if (p.cfg.dry_run) { void run(); return }
    setConfirm({
      text: `${what}?\n\nWriter: ${styleLabel}, duration ${p.cfg.duration || 'automatic'}.\n`
        + `Each story costs one writer call plus the narration (${p.cfg.tts_model}), both on OpenRouter. `
        + `Storyboards already done are skipped${p.cfg.force && !body.retry_failed ? " — except now, 'redo existing' is checked" : ''}.`,
      run: () => void run(),
    })
  }

  async function cancel() {
    if (!jobId) return
    setStatus('cancelling... (aborting requests)')
    try { await api.cancel(jobId) } catch { /* the job reports */ }
  }

  function retryRow(row: Record<string, string>) {
    start({ images: [row.source_image], style: row.style || 'descriptive' }, `Retry 1 failed storyboard(s)`)
  }

  function askDelete(row: Record<string, string>, audioOnly: boolean) {
    const folder = `${log?.output_dir ?? ''}/${row.folder}`
    setConfirm({
      text: `Delete ${audioOnly ? 'only the AUDIO (audio.wav) of' : 'the whole production (script, audio, story.json and the storyboard copy) of'}\n\n“${row.title}”\n${folder}\n\n`
        + 'The original storyboard in the storyboards dir is NOT touched. Files go to the Trash when possible.'
        + (audioOnly ? '' : '\n\nThis storyboard will be written again the next time you generate this folder.'),
      run: async () => {
        setConfirm(null)
        try {
          const res = await api.storyDelete(folder, audioOnly)
          setStatus(`${res.method === 'trash' ? 'moved to the Trash' : 'deleted'}: ${audioOnly ? 'audio of ' : ''}${row.title}`)
          void refreshLog()
        } catch (e) {
          setMessage({ title: 'Delete', text: (e as Error).message })
        }
      },
    })
  }

  function showFailure(row: Record<string, string>) {
    setMessage({
      title: 'Storyboard failed',
      text: `${base(row.source_image || '')}\n${(row.date || '').slice(0, 19)}  writer ${row.writer_model}\n\n${row.error || '(no detail)'}`,
      actions: [{ label: `Retry this storyboard (${row.style || 'descriptive'})`, onClick: () => retryRow(row) }],
    })
  }

  function menuItems(row: Record<string, string>): MenuItem[] {
    const items: MenuItem[] = [{ label: 'Copy row', onClick: () => { void copyText(rowDetails(row)); setStatus('row copied to the clipboard') } },
      { label: '', separator: true }]
    if (row.status === 'error') {
      items.push({ label: 'Show error', onClick: () => showFailure(row) },
        { label: 'Retry this storyboard', onClick: () => retryRow(row) })
    } else if (row.status === 'deleted') {
      items.push({ label: '(production deleted)', disabled: true })
    } else {
      const folder = `${log?.output_dir ?? ''}/${row.folder}`
      items.push({ label: 'Open in Player', onClick: () => p.openInPlayer(folder) },
        { label: 'Open folder', onClick: () => void api.open(folder) },
        { label: '', separator: true })
      if (row.status !== 'no audio') items.push({ label: 'Delete audio only…', onClick: () => askDelete(row, true) })
      items.push({ label: 'Delete script + audio…', onClick: () => askDelete(row, false) })
    }
    return items
  }

  const rows: Row<Record<string, string>>[] = (log?.rows ?? []).map((row, i) => {
    const failed = row.status === 'error'
    const cells: Record<string, string> = {
      status: STATUS_LABEL[row.status || 'ok'] ?? '✔ ok',
      date: (row.date || '').slice(5, 16).replace('T', ' '),
      title: row.title || base(row.source_image || ''),
      storyboard_dir: dirLabel(row.source_image || ''),
      style: row.style || 'descriptive',
      scenes: row.scenes || '', voice_label: row.voice_label || '', audio_seconds: row.audio_seconds || '',
      target_seconds: row.target_seconds || '', writer_cost_usd: row.writer_cost_usd || '',
      tts_cost_usd: row.tts_cost_usd || '', detail: failed ? row.error || '' : row.folder || '',
    }
    return { id: `${i}-${row.date}`, cells, sort: { ...cells, date: row.date || '' },
      className: failed ? 'row-error' : undefined, payload: row }
  })
  const failures = log?.failed.length ?? 0
  const liveStatus = running
    ? `${progress}${step ? `  |  status: ${step} (${Math.round(stepSeconds)} s)` : ''}` : status

  // step seconds keep counting between server events
  useEffect(() => {
    if (!running) return
    const t = window.setInterval(() => setStepSeconds((s) => s + 0.5), 500)
    return () => window.clearInterval(t)
  }, [running, step])

  return (
    <div>
      <div className="card">
        {(['input_dir', 'output_dir'] as const).map((key) => (
          <div key={key}>
            <label className="field-label" title={key === 'input_dir'
              ? 'Folder with the storyboard images (.png/.jpg/.webp/.gif). Each image becomes one story.'
              : 'Where each story folder (title) and log_story_generate.csv are saved.'}>
              {key === 'input_dir' ? 'Storyboards dir' : 'Output dir'} ⓘ
            </label>
            <div className="pick-row">
              <input type="text" value={p.cfg[key]} onChange={(e) => p.set(key, e.target.value)} />
              <button className="ghost" onClick={() => setPicker(key)}>Browse…</button>
            </div>
          </div>
        ))}
        <div className="story-opts">
          <label className="pill-select"><span>Language</span>
            <input list="story-langs" value={p.cfg.language} onChange={(e) => p.set('language', e.target.value)}
              style={{ width: 80 }} />
            <datalist id="story-langs">{p.meta.languages.map((l) => <option key={l} value={l} />)}</datalist>
          </label>
          <label className="pill-select" title="Descritivo: tells what each panel shows, faithful to the images. Narrativo: focuses on the transitions - fills in what happened between one panel and the next.">
            <span>Writer</span>
            <select value={p.cfg.style} onChange={(e) => p.set('style', e.target.value)}>
              {p.meta.styles.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </label>
          <label className="pill-select" title="Target narration length: empty = automatic; e.g. 90, 1:30 or 2m. Converted to words per voice using the speech speed measured on your previous stories.">
            <span>Duration</span>
            <input value={p.cfg.duration} onChange={(e) => p.set('duration', e.target.value)} style={{ width: 64 }} />
          </label>
          <label className={`pill-check${p.cfg.dry_run ? ' on' : ''}`} title="Offline test: placeholder story and silent audio, no key, no cost.">
            <input type="checkbox" checked={p.cfg.dry_run} onChange={(e) => p.set('dry_run', e.target.checked)} /> dry-run
          </label>
          <label className={`pill-check${p.cfg.force ? ' on' : ''}`} title="Storyboards that already have a story in the selected style are skipped to save money. Check to write them again (new folder).">
            <input type="checkbox" checked={p.cfg.force} onChange={(e) => p.set('force', e.target.checked)} /> redo existing
          </label>
        </div>
        <div className="story-actions">
          <button className="btn-generate" disabled={running}
            onClick={() => start({ input_dir: p.cfg.input_dir }, 'Write and narrate the storyboards of this folder')}>
            Generate stories
          </button>
          <button className="btn-cancel" onClick={() => void cancel()} disabled={!running}>Cancel</button>
          <button className="ghost" disabled={!failures || running}
            title="Run again only the storyboards whose last attempt failed (red rows below) and that still have no story."
            onClick={() => start({ retry_failed: true }, `Retry ${failures} failed storyboard(s)`)}>
            Retry failed{failures ? ` (${failures})` : ''}
          </button>
          <span className="elapsed">elapsed: {elapsed.toFixed(1)}s</span>
          {running && <span className="spinner"><div /></span>}
        </div>
        <div className="story-status" title="Progress (stories done / total) and the current step: writer, narration of each scene, saving, cost lookup, retries.">
          {liveStatus}
        </div>
      </div>

      <div className="card">
        <div className="log-head">
          <h3>Stories log (newest first)</h3>
          <span className="hint">red = failed: double-click for details / retry, right-click to copy the row</span>
          <button className="ghost" onClick={() => void refreshLog()}>Refresh</button>
        </div>
        <DataTable columns={COLUMNS} rows={rows}
          sort={parseSort(p.cfg.log_sort)} onSortChange={(s) => p.set('log_sort', formatSort(s))}
          onRowDoubleClick={(r) => r.payload.status === 'error' ? showFailure(r.payload)
            : r.payload.status !== 'deleted' && p.openInPlayer(`${log?.output_dir ?? ''}/${r.payload.folder}`)}
          onRowContextMenu={(r, x, y) => setMenu({ x, y, row: r.payload })}
          footer={(visible, total, filtered) => {
            const rs = visible.map((r) => r.payload)
            const ok = rs.filter((r) => ['ok', 'no audio', ''].includes(r.status ?? '')).length
            const failed = rs.filter((r) => r.status === 'error').length
            const cost = rs.reduce((s, r) => s + rowCost(r), 0)
            return `total: ${filtered ? `${visible.length} of ${total} rows: ` : ''}${ok} stories, ${failed} failed attempts / $${cost.toFixed(6)} (writer + narration)  (${log?.output_dir ?? ''})${filtered ? '  — filtered' : ''}`
          }} />
      </div>

      {menu && <ContextMenu x={menu.x} y={menu.y} items={menuItems(menu.row)} onClose={() => setMenu(null)} />}
      {picker && (
        <FolderPicker initial={p.cfg[picker]} title={picker === 'input_dir' ? 'storyboards dir' : 'output dir'}
          onPick={(dir) => { p.set(picker, dir); setPicker(null) }} onClose={() => setPicker(null)} />
      )}
      {confirm && (
        <div className="modal-bg" onClick={() => setConfirm(null)}>
          <div className="modal confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Confirm</h3>
            <p className="modal-text">{confirm.text}</p>
            <div className="modal-actions">
              <button className="btn-cancel" onClick={() => setConfirm(null)}>No</button>
              <button className="btn-generate" onClick={confirm.run}>Yes</button>
            </div>
          </div>
        </div>
      )}
      {message && (
        <div className="modal-bg" onClick={() => setMessage(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>{message.title}</h3>
            <p className="modal-text">{message.text}</p>
            <div className="modal-actions">
              {message.actions?.map((a) => (
                <button key={a.label} className="ghost" onClick={() => { setMessage(null); a.onClick?.() }}>{a.label}</button>
              ))}
              <button className="primary" onClick={() => setMessage(null)}>OK</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
