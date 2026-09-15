import { useEffect, useState } from 'react'
import { api, AppConfig, ProvidersResponse } from './api'
import Generate from './tabs/Generate'
import Model from './tabs/Model'
import Dir from './tabs/Dir'

const DEFAULTS: AppConfig = {
  output_dir: '', context_dir: '', memory_dir: '',
  provider: 'openrouter', model: '', summary_model: '',
  prop: '1:1', resolution: '1K', output_format: 'png', temperature: '', dry_run: false,
}

export default function App() {
  const [tab, setTab] = useState<'generate' | 'model' | 'dir'>('generate')
  const [cfg, setCfg] = useState<AppConfig>(DEFAULTS)
  const [meta, setMeta] = useState<ProvidersResponse | null>(null)
  const [prompt, setPrompt] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [rememberKey, setRememberKey] = useState(false)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    api.providers().then(setMeta).catch(() => setNotice('cannot reach API (is web/server.py running?)'))
    api.config().then((c) => setCfg({ ...DEFAULTS, ...c })).catch(() => undefined)
  }, [])

  function set<K extends keyof AppConfig>(key: K, value: AppConfig[K]) {
    const next = { ...cfg, [key]: value }
    setCfg(next)
    api.saveConfig(next).catch(() => undefined)
  }

  function usePrompt(text: string) {
    if (!text.trim()) return
    setPrompt(text.trim())
    setTab('generate')
    setNotice('prompt loaded from log')
  }

  return (
    <>
    <div className="page-bg" aria-hidden="true">
      <img src="/img/logo.png" alt="" />
    </div>
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img className="brand-logo" src="/img/logo.png" alt="ImageGenerate logo" />
          <span>
            <div className="brand-name">ImageGenerate</div>
            <div className="brand-sub">Generating your thoughts!</div>
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <nav className="tabs">
            {(['generate', 'model', 'dir'] as const).map((t) => (
              <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </nav>
          <a className="help-btn" href="/help" target="_blank" rel="noreferrer"
            title="Open docs (docs.html)">?</a>
        </div>
      </header>

      {tab === 'generate' && (
        <section className="hero">
          <div className="hero-copy">
            <div className="mono-label">AI image generate</div>
            <h1>Describe it. Generate it. Keep iterating.</h1>
            <p>Create images from your home quickly and easily.</p>
          </div>
        </section>
      )}

      {notice && <div className="notice">{notice}</div>}

      {tab === 'generate' && (
        <Generate
          outputDir={cfg.output_dir} provider={cfg.provider}
          model={cfg.model} summaryModel={cfg.summary_model}
          prop={cfg.prop} setProp={(v) => set('prop', v)}
          resolution={cfg.resolution} setResolution={(v) => set('resolution', v)}
          outputFormat={cfg.output_format} setOutputFormat={(v) => set('output_format', v)}
          dryRun={cfg.dry_run} setDryRun={(v) => set('dry_run', v)}
          aspectRatios={meta?.aspect_ratios ?? ['1:1']}
          resolutions={meta?.resolutions ?? ['1K']}
          outputFormats={meta?.output_formats ?? ['png']}
          apiKey={apiKey} rememberKey={rememberKey}
          onUsePrompt={usePrompt} prompt={prompt} setPrompt={setPrompt}
          temperature={cfg.temperature}
        />
      )}
      {tab === 'model' && (
        <Model
          provider={cfg.provider} setProvider={(v) => set('provider', v)}
          model={cfg.model} setModel={(v) => set('model', v)}
          summaryModel={cfg.summary_model} setSummaryModel={(v) => set('summary_model', v)}
          temperature={cfg.temperature} setTemperature={(v) => set('temperature', v)}
          apiKey={apiKey} setApiKey={setApiKey}
          rememberKey={rememberKey} setRememberKey={setRememberKey}
          providers={meta?.providers ?? []} status={setNotice}
        />
      )}
      {tab === 'dir' && (
        <Dir
          outputDir={cfg.output_dir} setOutputDir={(v) => set('output_dir', v)}
          contextDir={cfg.context_dir} setContextDir={(v) => set('context_dir', v)}
          memoryDir={cfg.memory_dir} setMemoryDir={(v) => set('memory_dir', v)}
        />
      )}
    </div>
    </>
  )
}
