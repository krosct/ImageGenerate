import { useState } from 'react'
import { api, listenJob, LogResponse } from '../api'

interface Props {
  outputDir: string
  provider: string
  model: string
  summaryModel: string
  prop: string
  setProp: (v: string) => void
  resolution: string
  setResolution: (v: string) => void
  outputFormat: string
  setOutputFormat: (v: string) => void
  dryRun: boolean
  setDryRun: (v: boolean) => void
  aspectRatios: string[]
  resolutions: string[]
  outputFormats: string[]
  apiKey: string
  rememberKey: boolean
  onUsePrompt: (text: string) => void
  prompt: string
  setPrompt: (v: string) => void
}

function ratioBox(prop: string): { w: number; h: number } | null {
  const m = prop.replace(/\s/g, '').split(':')
  if (m.length !== 2) return null
  const w = parseFloat(m[0])
  const h = parseFloat(m[1])
  if (!(w > 0 && h > 0)) return null
  const scale = Math.min(200 / w, 110 / h)
  return { w: w * scale, h: h * scale }
}

export default function Generate(p: Props) {
  const [running, setRunning] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [status, setStatus] = useState('idle')
  const [jobId, setJobId] = useState<string | null>(null)
  const [logOpen, setLogOpen] = useState(false)
  const [log, setLog] = useState<LogResponse | null>(null)
  const [done, setDone] = useState<{ images: string[]; cost: number } | null>(null)

  async function refreshLog() {
    try {
      setLog(await api.log(p.outputDir))
    } catch (e) {
      setStatus(`log error: ${(e as Error).message}`)
    }
  }

  async function onGenerate() {
    if (!p.prompt.trim()) { setStatus('type a prompt first'); return }
    if (!p.summaryModel.trim()) { setStatus('fill in Summary model first (Model tab)'); return }
    setRunning(true)
    setElapsed(0)
    setDone(null)
    setStatus('generating...')
    try {
      const { job_id } = await api.generate({
        prompt: p.prompt,
        output_dir: p.outputDir || null,
        provider: p.provider,
        model: p.model || null,
        summary_model: p.summaryModel,
        prop: p.prop,
        resolution: p.resolution,
        output_format: p.outputFormat,
        dry_run: p.dryRun,
        api_key: p.apiKey || null,
        remember_key: p.rememberKey,
      })
      setJobId(job_id)
      listenJob(job_id, (ev) => {
        if (ev.status === 'running') {
          setElapsed(ev.elapsed)
        } else if (ev.status === 'done') {
          setRunning(false)
          setElapsed(ev.result.elapsed)
          setStatus(`saved ${ev.result.images.length} image(s) | $${ev.result.cost.toFixed(6)}`)
          setDone({ images: ev.result.images, cost: ev.result.cost })
          setLogOpen(true)
          void refreshLog()
        } else if (ev.status === 'cancelled') {
          setRunning(false)
          setStatus('cancelled: partial files removed, nothing logged')
        } else {
          setRunning(false)
          setStatus(`error: ${ev.error}`)
        }
      })
    } catch (e) {
      setRunning(false)
      setStatus(`error: ${(e as Error).message}`)
    }
  }

  async function onCancel() {
    if (jobId) {
      setStatus('cancelling... (aborting requests)')
      try { await api.cancel(jobId) } catch { /* job will report */ }
    }
  }

  const box = ratioBox(p.prop)

  return (
    <div>
      <div className="card">
        <label>Prompt</label>
        <textarea value={p.prompt} onChange={(e) => p.setPrompt(e.target.value)} />
        <div className="row">
          <div>
            <label title="Aspect ratio appended to the prompt">Aspect (prop)
              <span className="ratio-tip">ⓘ
                {box && (
                  <span className="ratio-preview">
                    <svg width="216" height="126">
                      <rect x={(216 - box.w) / 2} y={(126 - box.h) / 2} width={box.w} height={box.h}
                        fill="none" stroke="red" strokeWidth="3" />
                    </svg>
                    <div style={{ color: '#000', fontSize: 12 }}>{p.prop}</div>
                  </span>
                )}
              </span>
            </label>
            <select value={p.prop} onChange={(e) => p.setProp(e.target.value)} disabled={running}>
              {p.aspectRatios.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </div>
          <div>
            <label>Resolution</label>
            <select value={p.resolution} onChange={(e) => p.setResolution(e.target.value)} disabled={running}>
              {p.resolutions.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <div>
            <label>Format</label>
            <select value={p.outputFormat} onChange={(e) => p.setOutputFormat(e.target.value)} disabled={running}>
              {p.outputFormats.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
          <div><label title="Test run without spending anything: writes a local placeholder instead of calling the paid API.">
            <input type="checkbox" checked={p.dryRun} onChange={(e) => p.setDryRun(e.target.checked)} disabled={running} /> dry-run</label></div>
        </div>
        <div style={{ marginTop: 12 }}>
          <button className="primary" onClick={onGenerate} disabled={running}>Generate</button>
          <button className="ghost" onClick={onCancel} disabled={!running} style={{ marginLeft: 8 }}>Cancel</button>
          <span className="clock" title="Time from sending the request until the image arrives.">
            elapsed: {elapsed.toFixed(1)}s
          </span>
          {running && <span className="spinner"><div /></span>}
          <span className="status" title="Current state: idle, generating, done, cancelled or error.">{status}</span>
          <button className="ghost" onClick={() => { setLogOpen(!logOpen); if (!logOpen) void refreshLog() }} style={{ float: 'right' }}>
            {logOpen ? 'Hide log ▲' : 'Show log ▼'}
          </button>
        </div>
      </div>

      {logOpen && (
        <div className="card">
          <button className="ghost" onClick={refreshLog} style={{ float: 'right' }}>Refresh log</button>
          <h3>log_image_generate.csv</h3>
          {!log && <div className="hint">loading…</div>}
          {log && (
            <>
              <div className="hint">total: {log.total_ops} ops / ${log.total_cost.toFixed(6)} · click a row to reuse its prompt</div>
              <div className="logwrap">
                <table className="log">
                  <thead><tr>{log.fields.map((f) => <th key={f}>{f.replace(/_/g, ' ')}</th>)}</tr></thead>
                  <tbody>
                    {log.rows.map((row, i) => (
                      <tr key={i} onClick={() => p.onUsePrompt(row.prompt_full || row.prompt_summary || '')}
                          title="Use prompt">
                        {log.fields.map((f) => <td key={f}>{(row[f] ?? '').slice(0, 120)}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {done && (
        <div className="modal-bg" onClick={() => setDone(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Imagem gerada com sucesso!</h3>
            {done.images.map((img) => {
              const name = img.split('/').pop() ?? img
              return (
                <div key={img}>
                  <img src={api.imageUrl(p.outputDir, name)} alt={name} />
                  <div><a href={api.imageUrl(p.outputDir, name)} target="_blank" rel="noreferrer">
                    <button className="ghost">Abrir</button>
                  </a> <span className="hint">{name}</span></div>
                </div>
              )
            })}
            <div style={{ marginTop: 12, textAlign: 'right' }}>
              <button className="primary" onClick={() => setDone(null)}>OK</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
