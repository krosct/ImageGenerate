import { useEffect, useRef, useState } from 'react'
import { api, StoryInfo } from '../api'

// Web twin of the desktop StoryGenerate "Player" tab: story list | storyboard
// (zoom at the pointer, drag to pan, right-click resets) | script (current
// scene highlighted, click to seek) + transport controls and shortcuts.
interface Props {
  outputDir: string
  selected: string
  setSelected: (folder: string) => void
  active: boolean
  refreshKey: number
  status: (text: string) => void
}

function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds || 0))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function sceneAt(scenes: StoryInfo['scenes'], pos: number): number {
  let index = 0
  scenes.forEach((scene, i) => { if (pos >= Number(scene.start ?? 0)) index = i })
  return index
}

// same limit as the desktop Player: up to 3x the original image pixels
const MIN_ZOOM = 1, MAX_ORIGINAL = 3

export default function StoryPlayer(p: Props) {
  const [stories, setStories] = useState<StoryInfo[]>([])
  const [pos, setPos] = useState(0)
  const [duration, setDuration] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [seeking, setSeeking] = useState<number | null>(null)
  const [widths, setWidths] = useState<[number, number]>([200, 300])
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 })
  const audio = useRef<HTMLAudioElement>(null)
  const stage = useRef<HTMLDivElement>(null)
  const scriptBox = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null)

  async function refresh() {
    try {
      const res = await api.stories(p.outputDir)
      setStories(res.stories)
      if (res.stories.length && !res.stories.some((s) => s.folder === p.selected)) {
        p.setSelected(res.stories[0].folder)
      }
    } catch (e) {
      p.status(`player: ${(e as Error).message}`)
    }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void refresh() }, [p.outputDir, p.refreshKey])

  const story = stories.find((s) => s.folder === p.selected) ?? null
  const scenes = story?.scenes ?? []
  const current = scenes.length ? sceneAt(scenes, pos) : -1

  useEffect(() => {
    setView({ zoom: 1, x: 0, y: 0 }); setPos(0); setPlaying(false)
    setDuration(story?.audio_seconds ?? 0)
  }, [story?.folder, story?.audio_seconds])

  useEffect(() => {
    scriptBox.current?.querySelector('.scene-current')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [current])

  function seek(seconds: number) {
    const a = audio.current
    if (!a) return
    a.currentTime = Math.max(0, Math.min(seconds, a.duration || duration || seconds))
    setPos(a.currentTime)
  }
  function toggle() {
    const a = audio.current
    if (!a || !story?.audio) return
    if (a.paused) void a.play().catch((e) => p.status(`player: ${e.message}`))
    else a.pause()
  }
  function stop() {
    const a = audio.current
    if (!a) return
    a.pause(); a.currentTime = 0; setPos(0)
  }
  function skip(delta: number) { if (audio.current) seek(audio.current.currentTime + delta) }
  function jumpScene(step: number) {
    if (!scenes.length || !audio.current) return
    const now = audio.current.currentTime
    const index = sceneAt(scenes, now)
    // "previous" restarts the current scene unless we are at its very start
    const target = step < 0 && now - Number(scenes[index].start ?? 0) > 2
      ? index : Math.max(0, Math.min(scenes.length - 1, index + step))
    seek(Number(scenes[target].start ?? 0))
  }

  // keyboard: space, ←/→ 5 s, ↑/↓ scene (not while typing in a field)
  useEffect(() => {
    if (!p.active) return
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) return
      const actions: Record<string, () => void> = {
        ' ': toggle, ArrowLeft: () => skip(-5), ArrowRight: () => skip(5),
        ArrowUp: () => jumpScene(-1), ArrowDown: () => jumpScene(1),
      }
      const action = actions[e.key]
      if (!action) return
      e.preventDefault()
      action()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // ---- storyboard zoom / pan (CSS transform on the original image) ----
  function onWheel(e: React.WheelEvent) {
    const box = stage.current?.getBoundingClientRect()
    if (!box) return
    const img = stage.current?.querySelector('img')
    const maxZoom = img && img.clientWidth ? Math.max(1, MAX_ORIGINAL * img.naturalWidth / img.clientWidth) : 1
    const cx = e.clientX - box.left - box.width / 2
    const cy = e.clientY - box.top - box.height / 2
    setView((v) => {
      const zoom = Math.max(MIN_ZOOM, Math.min(maxZoom, v.zoom * (e.deltaY < 0 ? 1.25 : 0.8)))
      if (zoom === MIN_ZOOM) return { zoom, x: 0, y: 0 }
      // keep the point under the pointer still: pan' = c - (c - pan) * new / old
      const k = zoom / v.zoom
      return { zoom, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k }
    })
  }
  useEffect(() => {
    // React's onWheel is passive: block the page scroll natively
    const el = stage.current
    if (!el) return
    const block = (e: WheelEvent) => e.preventDefault()
    el.addEventListener('wheel', block, { passive: false })
    return () => el.removeEventListener('wheel', block)
  }, [story?.folder])

  function onMouseDown(e: React.MouseEvent) {
    if (e.button !== 0) return
    e.preventDefault()
    drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }
    const move = (ev: MouseEvent) => {
      const d = drag.current
      if (d) setView((v) => ({ ...v, x: d.vx + ev.clientX - d.x, y: d.vy + ev.clientY - d.y }))
    }
    const up = () => {
      drag.current = null
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  // ---- draggable dividers between list | image | script ----
  function startResize(which: 0 | 1, e: React.MouseEvent) {
    e.preventDefault()
    const startX = e.clientX, start = widths
    const move = (ev: MouseEvent) => {
      const dx = ev.clientX - startX
      setWidths(which === 0
        ? [Math.max(160, start[0] + dx), start[1]]
        : [start[0], Math.max(200, start[1] - dx)])
    }
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      document.body.style.cursor = ''
    }
    document.body.style.cursor = 'col-resize'
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const shownPos = seeking ?? pos
  const scene = current >= 0 ? scenes[current] : null

  return (
    <div className="card player">
      <div className="player-panes" style={{ gridTemplateColumns: `${widths[0]}px 6px 1fr 6px ${widths[1]}px` }}>
        <div className="player-list">
          <div className="field-label">Stories (newest first)</div>
          <div className="player-list-box">
            {stories.map((s) => (
              <div key={s.folder} className={`player-item${s.folder === p.selected ? ' active' : ''}`}
                title={s.folder} onClick={() => p.setSelected(s.folder)}>{s.label}</div>
            ))}
            {!stories.length && <div className="hint" style={{ padding: 8 }}>no stories in {p.outputDir}</div>}
          </div>
          <button className="ghost" onClick={() => void refresh()}>Refresh</button>
          <button className="ghost" disabled={!story} onClick={() => story && void api.open(story.folder)}>Open folder</button>
        </div>
        <div className="splitter" onMouseDown={(e) => startResize(0, e)} title="Drag to resize" />
        <div className="player-main">
            <div className="player-stage" ref={stage} onWheel={onWheel} onMouseDown={onMouseDown}
              onContextMenu={(e) => { e.preventDefault(); setView({ zoom: 1, x: 0, y: 0 }) }}>
              {story?.image
                ? <img src={api.fileUrl(story.image)} alt={story.title} draggable={false}
                    style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }} />
                : <div className="hint">{story ? 'storyboard image not found' : ''}</div>}
              {story?.image && (
                <div className="stage-hint">wheel: zoom at the pointer · drag: move · right-click: reset
                  {view.zoom !== 1 ? ` · ${Math.round(view.zoom * 100)}%` : ''}</div>
              )}
            </div>

            <div className="player-controls">
              <button className="ghost" title="Previous scene" onClick={() => jumpScene(-1)}>⏮</button>
              <button className="ghost" title="Back 10 seconds (Left arrow: 5 s)" onClick={() => skip(-10)}>⏪ 10s</button>
              <button className="ghost" title="Play / Pause (Space)" onClick={toggle} disabled={!story?.audio}>{playing ? '⏸' : '▶'}</button>
              <button className="ghost" title="Forward 10 seconds (Right arrow: 5 s)" onClick={() => skip(10)}>10s ⏩</button>
              <button className="ghost" title="Next scene" onClick={() => jumpScene(1)}>⏭</button>
              <button className="ghost" title="Stop (back to the start)" onClick={stop}>⏹</button>
              <input type="range" className="seek" min={0} max={Math.max(0.1, duration)} step={0.1}
                value={shownPos} disabled={!story?.audio}
                onChange={(e) => setSeeking(Number(e.target.value))}
                onMouseUp={() => { if (seeking !== null) seek(seeking); setSeeking(null) }}
                onKeyUp={() => { if (seeking !== null) seek(seeking); setSeeking(null) }} />
              <span className="time">
                {story && !story.audio ? (story.audio_deleted ? 'no audio' : 'audio error') : `${clock(shownPos)} / ${clock(duration)}`}
              </span>
            </div>
            <div className="hint scene-line">
              {scene ? `${story?.scene_word} ${scene.number}/${scenes.length}${scene.heading ? ` — ${scene.heading}` : ''}` : ''}
              {story && !story.audio && story.audio_deleted ? 'this story has no audio (deleted)' : ''}
            </div>
        </div>
        <div className="splitter" onMouseDown={(e) => startResize(1, e)} title="Drag to resize" />
        <div className="player-script" ref={scriptBox}>
          {story && (
            <>
              <h2>{story.title}</h2>
              {story.logline && <p className="logline">{story.logline}</p>}
              {scenes.map((sc, i) => (
                <div key={i} className={`scene${i === current ? ' scene-current' : ''}`}
                  onClick={() => seek(Number(sc.start ?? 0))} title="Click to play from this scene">
                  <div className="scene-heading">
                    {story.scene_word} {sc.number}{sc.heading ? ` — ${sc.heading}` : ''}  ({clock(Number(sc.start ?? 0))})
                  </div>
                  <div>{sc.narration}</div>
                </div>
              ))}
              {story.voice_id && (
                <p className="voice-line">Voz: {story.voice_label || story.voice_id}
                  {story.voice_reason ? ` — ${story.voice_reason}` : ''}</p>
              )}
            </>
          )}
        </div>
      </div>

      {story?.audio && (
        <audio ref={audio} src={api.fileUrl(story.audio)} preload="metadata"
          onTimeUpdate={(e) => setPos(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || story.audio_seconds)}
          onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} />
      )}
    </div>
  )
}
