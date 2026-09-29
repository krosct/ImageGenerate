import { useEffect, useState } from 'react'
import { api, StoryConfig, StoryMeta, StoryVoice } from '../api'
import StoryGenerateTab from './StoryGenerateTab'
import StoryPlayer from './StoryPlayer'

// StoryGenerate in the web UI: same tabs as the desktop app (story_generate.py
// --gui) — Generate / Model / Voices / Player — sharing story_config.json.
type Tab = 'generate' | 'model' | 'voices' | 'player'

const EMPTY: StoryConfig = {
  input_dir: '', output_dir: '', writer_model: '', tts_model: '', language: 'pt-BR',
  voices: [], dry_run: false, force: false, style: 'descriptive', duration: '', log_sort: '',
}

export default function StoryApp({ notice }: { notice: (text: string) => void }) {
  const [tab, setTab] = useState<Tab>('generate')
  const [cfg, setCfg] = useState<StoryConfig>(EMPTY)
  const [meta, setMeta] = useState<StoryMeta | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [rememberKey, setRememberKey] = useState(false)
  const [keyInfo, setKeyInfo] = useState<{ configured: boolean; source: string | null; vault_dir: string } | null>(null)
  const [voiceInfo, setVoiceInfo] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)

  async function reloadKeys() {
    try { setKeyInfo((await api.keys()).openrouter ?? null) } catch { /* offline */ }
  }
  useEffect(() => {
    api.storyMeta().then(setMeta).catch(() => notice('cannot reach API (is web/server.py running?)'))
    api.storyConfig().then((c) => setCfg({ ...EMPTY, ...c })).catch(() => undefined)
    void reloadKeys()
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  function set<K extends keyof StoryConfig>(key: K, value: StoryConfig[K]) {
    setCfg((old) => ({ ...old, [key]: value }))
    // only the changed key: the server merges into story_config.json (shared with the GUI)
    api.saveStoryConfig({ [key]: value } as Partial<StoryConfig>).catch(() => undefined)
  }

  const maxVoices = meta?.max_voices ?? 5
  const voiceRows: StoryVoice[] = Array.from({ length: maxVoices }, (_, i) => cfg.voices[i] ?? { id: '', label: '' })

  function setVoice(index: number, field: 'id' | 'label', value: string) {
    const rows = voiceRows.map((v, i) => (i === index ? { ...v, [field]: value } : v))
    setCfg((old) => ({ ...old, voices: rows }))
    // same shape the desktop saves: only the rows with an id, in order
    api.saveStoryConfig({ voices: rows.filter((v) => v.id.trim()).map((v) => ({ id: v.id.trim(), label: v.label.trim() })) })
      .catch(() => undefined)
  }

  async function checkVoices() {
    const voices = voiceRows.filter((v) => v.id.trim()).map((v) => ({ id: v.id.trim(), label: v.label.trim() }))
    setVoiceInfo({})
    try {
      const res = await api.checkVoices(voices)
      const info: Record<string, string> = {}
      for (const v of res.voices) {
        info[v.id] = v.title
          ? `${v.title} | ${(v.languages ?? []).join(',') || '?'} | ${(v.tags ?? []).slice(0, 4).join(', ')}`
          : 'not found (check the voice id)'
      }
      setVoiceInfo(info)
    } catch (e) {
      notice(`Voices: ${(e as Error).message}`)
    }
  }

  async function onForget() {
    try {
      const res = await api.forgetKey('openrouter')
      setRememberKey(false); setApiKey('')
      notice(`${res.forgotten ? 'forgot' : 'no'} remembered openrouter key`)
      await reloadKeys()
    } catch (e) {
      notice(`forget error: ${(e as Error).message}`)
    }
  }

  function openInPlayer(folder: string) {
    setSelected(folder)
    setRefreshKey((k) => k + 1)
    setTab('player')
  }

  if (!meta) return <div className="card">loading StoryGenerate…</div>

  return (
    <>
      <nav className="tabs story-tabs">
        {(['generate', 'model', 'voices', 'player'] as const).map((t) => (
          <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      <div style={{ display: tab === 'generate' ? undefined : 'none' }}>
        <StoryGenerateTab cfg={cfg} meta={meta} set={set} apiKey={apiKey} rememberKey={rememberKey}
          openInPlayer={openInPlayer} goVoices={() => setTab('voices')} refreshKey={refreshKey} />
      </div>

      {tab === 'model' && (
        <div className="card">
          <label className="field-label" title={`OpenRouter chat model that SEES the storyboard (vision) and writes the story + picks the voice. Default ${meta.writer_default_model}.\nFallbacks: separate models with ';' - e.g. 'qwen/qwen3.8-27b:free;google/gemini-3.7-flash' tries the first (with its 3 retries) and, if it fails, the next one, and so on. The log shows which model wrote each story.`}>
            Writer model ⓘ</label>
          <input type="text" value={cfg.writer_model} placeholder={meta.writer_default_model}
            onChange={(e) => set('writer_model', e.target.value)} />
          <label className="field-label" title={`Fish Audio voice model served by OpenRouter (same key as the writer). ${meta.default_tts_model} is free (no availability guarantee); the others are paid per character.`}>
            TTS model ⓘ</label>
          <select className="full" value={cfg.tts_model} onChange={(e) => set('tts_model', e.target.value)}>
            {meta.tts_models.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <label className="field-label" title="One key for the writer AND the narrator. Same OpenRouter key/vault as ImageGenerate.">
            OpenRouter key{' '}
            {keyInfo?.configured
              ? <span className="key-ok">(configured via {keyInfo.source})</span>
              : <span className="key-bad">(not configured)</span>} ⓘ
          </label>
          <div className="pick-row">
            <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)}
              placeholder="leave empty to use the env var or the saved key" autoComplete="off" />
            <label className={`pill-check${rememberKey ? ' on' : ''}`}
              title={`Encrypt and save this key in ${keyInfo?.vault_dir ?? ''}/openrouter_api_key.enc when you generate.`}>
              <input type="checkbox" checked={rememberKey} onChange={(e) => setRememberKey(e.target.checked)} /> remember me
            </label>
            <button className="ghost" onClick={() => void onForget()}
              title="Delete the saved OpenRouter key (ImageGenerate uses the same one).">Forget</button>
          </div>
        </div>
      )}

      {tab === 'voices' && (
        <div className="card">
          <p className="hint">Up to {maxVoices} Fish Audio voices. The writer picks the most fitting one for each story.</p>
          <div className="voices-grid">
            {voiceRows.map((v, i) => (
              <div className="voice-row" key={i}>
                <span>Voice {i + 1}:</span>
                <input type="text" value={v.id} placeholder="voice id"
                  title="Fish Audio voice id: the code in the voice page URL (fish.audio/m/<id>)."
                  onChange={(e) => setVoice(i, 'id', e.target.value)} />
                <input type="text" value={v.label} placeholder="label (optional)"
                  title="Optional label to help the writer choose, e.g. 'narradora calma', 'menino animado', 'avô'."
                  onChange={(e) => setVoice(i, 'label', e.target.value)} />
                <span className="hint">{voiceInfo[v.id.trim()] ?? ''}</span>
              </div>
            ))}
          </div>
          <button className="ghost" onClick={() => void checkVoices()}>Check voices</button>
        </div>
      )}

      {tab === 'player' && (
        <StoryPlayer outputDir={cfg.output_dir} selected={selected} setSelected={setSelected}
          active={tab === 'player'} refreshKey={refreshKey} status={notice} />
      )}
    </>
  )
}
