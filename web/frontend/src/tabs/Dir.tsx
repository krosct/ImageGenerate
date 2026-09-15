import { useEffect, useState } from 'react'
import { api } from '../api'

interface Props {
  outputDir: string
  setOutputDir: (v: string) => void
  contextDir: string
  setContextDir: (v: string) => void
  memoryDir: string
  setMemoryDir: (v: string) => void
}

export default function Dir(p: Props) {
  const fields: [string, string, (v: string) => void, string][] = [
    ['Output dir', p.outputDir, p.setOutputDir,
      'Folder where generated images and log_image_generate.csv are saved.'],
    ['Context dir', p.contextDir, p.setContextDir,
      'Folder with .md/.txt files automatically added to the prompt as context.'],
    ['Memory dir', p.memoryDir, p.setMemoryDir,
      'Folder with reference images sent along with the prompt to guide generation.'],
  ]
  const [pickIndex, setPickIndex] = useState<number | null>(null)

  return (
    <div className="card">
      {fields.map(([label, value, setValue, hint], i) => (
        <div key={label}>
          <label className="field-label" title={hint}>{label} ⓘ</label>
          <div className="pick-row">
            <input type="text" value={value} onChange={(e) => setValue(e.target.value)}
              placeholder="Type, paste, or browse…" />
            <button className="ghost" onClick={() => setPickIndex(i)}>Browse…</button>
          </div>
        </div>
      ))}
      <div className="hint">Browse navigates folders on this machine (the app runs locally).</div>
      {pickIndex !== null && (
        <FolderPicker
          initial={fields[pickIndex][1]}
          title={fields[pickIndex][0]}
          onPick={(dir) => { fields[pickIndex][2](dir); setPickIndex(null) }}
          onClose={() => setPickIndex(null)}
        />
      )}
    </div>
  )
}

function FolderPicker({ initial, title, onPick, onClose }: {
  initial: string
  title: string
  onPick: (dir: string) => void
  onClose: () => void
}) {
  const [path, setPath] = useState<string | null>(null)
  const [parent, setParent] = useState('')
  const [home, setHome] = useState('')
  const [dirs, setDirs] = useState<string[]>([])
  const [error, setError] = useState('')
  const [newName, setNewName] = useState('')

  async function load(target: string) {
    setError('')
    try {
      const res = await api.browse(target)
      setPath(res.path)
      setParent(res.parent)
      setHome(res.home)
      setDirs(res.dirs)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  // Load once on open: current value, else home.
  useEffect(() => { void load(initial.trim() || '~') }, [])

  async function onMkdir() {
    if (!path || !newName.trim()) return
    try {
      const res = await api.mkdir(path, newName.trim())
      setNewName('')
      await load(res.path)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal picker-modal" onClick={(e) => e.stopPropagation()}>
        <h3>Choose {title.toLowerCase()}</h3>
        <code className="crumb" title={path ?? ''}>{path ?? 'loading…'}</code>
        {error && <div className="notice" style={{ marginTop: 8 }}>{error}</div>}
        <div className="picker-actions">
          <button className="ghost" onClick={() => void load(parent)} disabled={!path || path === parent}>⬆ Up</button>
          <button className="ghost" onClick={() => void load(home)} disabled={!home}>⌂ Home</button>
          <span className="hint">{dirs.length} folders</span>
        </div>
        <div className="dir-list">
          {dirs.map((d) => (
            <button key={d} className="dir-item" onClick={() => void load(`${path}/${d}`)} title={d}>
              <span aria-hidden="true">📁</span> {d}
            </button>
          ))}
          {path && dirs.length === 0 && <div className="hint">No subfolders.</div>}
        </div>
        <div className="pick-row" style={{ marginTop: 10 }}>
          <input type="text" value={newName} onChange={(e) => setNewName(e.target.value)}
            placeholder="New folder name…" onKeyDown={(e) => { if (e.key === 'Enter') void onMkdir() }} />
          <button className="ghost" onClick={() => void onMkdir()} disabled={!newName.trim()}>Create</button>
        </div>
        <div style={{ marginTop: 12, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" onClick={() => path && onPick(path)} disabled={!path}>
            Use this folder
          </button>
        </div>
      </div>
    </div>
  )
}
