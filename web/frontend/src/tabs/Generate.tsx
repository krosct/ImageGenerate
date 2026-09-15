import { useRef, useState } from 'react'
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
  temperature: string
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
  const [muted, setMuted] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const [countText, setCountText] = useState('1')
  const audioRef = useRef<AudioContext | null>(null)

  // Lazily created on the Generate click (a user gesture), so the
  // browser allows playback later when the async job finishes.
  function ensureAudio(): AudioContext | null {
    try {
      if (!audioRef.current) {
        const Ctor = window.AudioContext
          || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
        if (!Ctor) return null
        audioRef.current = new Ctor()
      }
      if (audioRef.current.state === 'suspended') void audioRef.current.resume()
      return audioRef.current
    } catch {
      return null
    }
  }

  // Small synthesized chime — no audio file needed.
  function playAlert(kind: 'success' | 'error') {
    if (muted) return
    const ctx = ensureAudio()
    if (!ctx) return
    try {
      const notes = kind === 'success' ? [659.25, 880.0] : [220.0, 164.81]
      notes.forEach((freq, i) => {
        const osc = ctx.createOscillator()
        const gain = ctx.createGain()
        const t0 = ctx.currentTime + i * 0.16
        osc.type = 'sine'
        osc.frequency.value = freq
        gain.gain.setValueAtTime(0.0001, t0)
        gain.gain.exponentialRampToValueAtTime(0.25, t0 + 0.03)
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.3)
        osc.connect(gain).connect(ctx.destination)
        osc.start(t0)
        osc.stop(t0 + 0.32)
      })
    } catch {
      /* audio is best-effort only */
    }
  }

  async function copyPath(path: string) {
    try {
      await navigator.clipboard.writeText(path)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = path
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
    }
    setCopied(path)
    window.setTimeout(() => setCopied((c) => (c === path ? null : c)), 1600)
  }

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
    const tempRaw = p.temperature.trim()
    if (tempRaw !== '') {
      const tempNum = Number(tempRaw)
      if (!Number.isFinite(tempNum) || tempNum < 0 || tempNum > 2) {
        setStatus('error: invalid temperature (need a number 0-2, or blank)')
        return
      }
    }
    if (!/^[0-9]+$/.test(countText.trim())) {
      setStatus('error: invalid count (need a natural number 1-10)')
      return
    }
    const count = parseInt(countText.trim(), 10)
    if (count < 1 || count > 10) {
      setStatus('error: invalid count (need a natural number 1-10)')
      return
    }
    if (count > 1 && !window.confirm(
      `Generate ${count} images with the same prompt and settings?\n\n` +
      `Each image counts as a separate generation and may incur additional ` +
      `costs (total ≈ ${count}× the single-image cost).\n\nContinue?`,
    )) return
    ensureAudio() // unlock sound on user gesture; chime plays when done
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
        temperature: tempRaw === '' ? null : tempRaw,
        count,
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
          playAlert('success')
          setLogOpen(true)
          void refreshLog()
        } else if (ev.status === 'cancelled') {
          setRunning(false)
          setStatus('cancelled: partial files removed, nothing logged')
        } else {
          setRunning(false)
          setStatus(`error: ${ev.error}`)
          playAlert('error')
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
  const statusClass = status.startsWith('error') || status.startsWith('log error')
    ? 'status is-error'
    : status.startsWith('saved')
      ? 'status is-done'
      : 'status'

  return (
    <div>
      <div className="composer">
        <textarea
          value={p.prompt}
          onChange={(e) => p.setPrompt(e.target.value)}
          placeholder="Describe the image… e.g. a red panda astronaut, cinematic light, ultra detailed"
        />
        <div className="composer-toolbar">
          <label className="pill-select">
            <span title="Aspect ratio appended to the prompt">Ratio
              <span className="ratio-tip"> ⓘ
                {box && (
                  <span className="ratio-preview">
                    <svg width="216" height="126">
                      <rect x={(216 - box.w) / 2} y={(126 - box.h) / 2} width={box.w} height={box.h}
                        fill="none" stroke="#111" strokeWidth="3" />
                    </svg>
                    <div style={{ color: '#000', fontSize: 12 }}>{p.prop}</div>
                  </span>
                )}
              </span>
            </span>
            <select value={p.prop} onChange={(e) => p.setProp(e.target.value)} disabled={running}>
              {p.aspectRatios.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
          <label className="pill-select">
            <span>Res</span>
            <select value={p.resolution} onChange={(e) => p.setResolution(e.target.value)} disabled={running}>
              {p.resolutions.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <label className="pill-select">
            <span>Fmt</span>
            <select value={p.outputFormat} onChange={(e) => p.setOutputFormat(e.target.value)} disabled={running}>
              {p.outputFormats.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </label>
          <label className={`pill-check${p.dryRun ? ' on' : ''}`}
            title="Test run without spending anything: writes a local placeholder instead of calling the paid API.">
            <input type="checkbox" checked={p.dryRun} onChange={(e) => p.setDryRun(e.target.checked)} disabled={running} /> dry-run
          </label>
          <div className="composer-actions">
            <button className="ghost" onClick={onCancel} disabled={!running}>Cancel</button>
            <label className="count-pill" title="How many images to generate with the same prompt (natural number 1-10). Above 1 asks for confirmation: each image may add costs.">
            <input type="text" value={countText} inputMode="numeric"
              onChange={(e) => { if (/^[0-9]*$/.test(e.target.value)) setCountText(e.target.value) }}
              disabled={running} aria-label="Image count" />×
          </label>
            <button className="primary" onClick={onGenerate} disabled={running}>
              {running ? 'Generating…' : 'Generate'}
            </button>
          </div>
        </div>
        <div className="run-strip">
          <span className="elapsed" title="Time from sending the request until the image arrives.">
            {elapsed.toFixed(1)}s
          </span>
          {running && <span className="spinner"><div /></span>}
          <span className={statusClass} title="Current state: idle, generating, done, cancelled or error.">{status}</span>
          <button className="ghost" onClick={() => setMuted(!muted)}
            title={muted ? 'Unmute alert sound' : 'Mute alert sound'}>
            {muted ? '🔕' : '🔔'}
          </button>
          <span style={{ marginLeft: 'auto' }}>
            <button className="ghost" onClick={() => { setLogOpen(!logOpen); if (!logOpen) void refreshLog() }}>
              {logOpen ? 'Hide log ▲' : 'Show log ▼'}
            </button>
          </span>
        </div>
      </div>

      {logOpen && (
        <div className="card">
          <div className="log-head">
            <h3>History</h3>
            <span className="hint">log_image_generate.csv</span>
            <span className="hint">click a row to reuse its prompt</span>
            <button className="ghost" onClick={refreshLog}>Refresh log</button>
          </div>
          {!log && <div className="hint">loading…</div>}
          {log && (
            <>
              <div className="hint">total: {log.total_ops} ops / ${log.total_cost.toFixed(6)}</div>
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
          <div className="modal success-modal" onClick={(e) => e.stopPropagation()}>
            <div className="success-head">
              <span className="success-badge" aria-hidden="true">✓</span>
              <div>
                <h3>Image generated!</h3>
                <div className="hint">
                  {done.images.length} image{done.images.length === 1 ? '' : 's'} saved · ${done.cost.toFixed(6)}
                </div>
              </div>
            </div>
            <div className="result-grid">
            {done.images.map((img) => {
              const name = img.split('/').pop() ?? img
              return (
                <div key={img} className="result-card">
                  <img src={api.imageUrl(p.outputDir, name)} alt={name} />
                  <div className="path-row" title={img}>
                    <code className="path-text">{img}</code>
                    <button className="ghost" onClick={() => void copyPath(img)}>
                      {copied === img ? 'Copied ✓' : 'Copy path'}
                    </button>
                  </div>
                  <div><a href={api.imageUrl(p.outputDir, name)} target="_blank" rel="noreferrer">
                    <button className="ghost">Open full size</button>
                  </a> <span className="hint">{name}</span></div>
                </div>
              )
            })}
            </div>
            <div style={{ marginTop: 12, textAlign: 'right' }}>
              <button className="primary" onClick={() => setDone(null)}>OK</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
