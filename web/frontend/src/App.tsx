import { useEffect, useRef, useState } from 'react'
import { api, retry, AppConfig, DynamicDirs, ProvidersResponse } from './api'
import Generate from './tabs/Generate'
import Model from './tabs/Model'
import Dir from './tabs/Dir'
import Injection from './tabs/Injection'
import Analyse from './tabs/Analyse'
import StoryApp, { type StoryTab } from './story/StoryApp'
import { extractTemplateVars, parseCountText } from './injection'
import { formatSort, parseSort } from './tableView'

const DEFAULTS: AppConfig = {
  output_dir: '', context_dir: '', memory_dir: '',
  provider: 'openrouter', model: '', summary_model: '',
  prop: '1:1', resolution: '1K', output_format: 'png', dry_run: false,
}

type AppName = 'image' | 'story'

export default function App() {
  // which app is shown: ImageGenerate or StoryGenerate (#story in the URL)
  const [app, setApp] = useState<AppName>(window.location.hash === '#story' ? 'story' : 'image')
  const [tab, setTab] = useState<'generate' | 'model' | 'dir' | 'analyse' | 'injection'>('generate')
  const [storyTab, setStoryTab] = useState<StoryTab>('generate')
  const [injectionCells, setInjectionCells] = useState<string[][]>([])
  const [cfg, setCfg] = useState<AppConfig>(DEFAULTS)
  const [meta, setMeta] = useState<ProvidersResponse | null>(null)
  const [prompt, setPrompt] = useState('')
  const promptLoaded = useRef(false)
  const [apiKey, setApiKey] = useState('')
  const [rememberKey, setRememberKey] = useState(false)
  const [notice, setNotice] = useState('')
  const [countText, setCountText] = useState('1')
  const [dynamicDirs, setDynamicDirs] = useState<DynamicDirs>({
    output_dir: { enabled: false, start: '1', range: '', batch: '1' },
    context_dir: { enabled: false, start: '1', range: '', batch: '1' },
    memory_dir: { enabled: false, start: '1', range: '', batch: '1' },
  })

  useEffect(() => {
    retry(() => api.providers()).then(setMeta).catch(() => setNotice('cannot reach API (is web/server.py running?)'))
    api.config().then((c) => {
      setCfg({ ...DEFAULTS, ...c })
      if (c.prompt) setPrompt(c.prompt)  // restore the last prompt (web or desktop)
      promptLoaded.current = true
    }).catch(() => { promptLoaded.current = true })
  }, [])

  function set<K extends keyof AppConfig>(key: K, value: AppConfig[K]) {
    setCfg((old) => ({ ...old, [key]: value }))
    // only the changed key: the server merges into config.json (shared with the GUI)
    api.saveConfig({ [key]: value } as Partial<AppConfig>).catch(() => undefined)
  }

  // Remember the prompt like the desktop GUI does (debounced while typing).
  useEffect(() => {
    if (!promptLoaded.current) return
    const timer = window.setTimeout(() => {
      api.saveConfig({ prompt }).catch(() => undefined)
    }, 800)
    return () => window.clearTimeout(timer)
  }, [prompt])

  function usePrompt(text: string) {
    if (!text.trim()) return
    setPrompt(text.trim())
    setTab('generate')
    setNotice('prompt loaded from log')
  }

  function switchApp(next: AppName) {
    setApp(next)
    setNotice('')
    window.history.replaceState(null, '', next === 'story' ? '#story' : window.location.pathname)
    document.title = next === 'story' ? 'StoryGenerate' : 'ImageGenerate — Studio'
  }
  useEffect(() => { if (app === 'story') document.title = 'StoryGenerate' }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const templateVars = extractTemplateVars(prompt)
  const injectionVisible = parseCountText(countText) > 1 && templateVars.length > 0

  useEffect(() => {
    if (tab === 'injection' && !injectionVisible) setTab('generate')
  }, [tab, injectionVisible])

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
            <div className="brand-name">{app === 'story' ? 'StoryGenerate' : 'ImageGenerate'}</div>
            <div className="brand-sub">{app === 'story' ? 'Storyboards into narrated stories' : 'Generating your thoughts!'}</div>
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <div className="app-switch" role="tablist" title="Switch between the two apps (same as the two desktop launchers)">
            <button className={app === 'image' ? 'active' : ''} onClick={() => switchApp('image')}>ImageGenerate</button>
            <button className={app === 'story' ? 'active' : ''} onClick={() => switchApp('story')}>StoryGenerate</button>
          </div>
          {app === 'image' && <nav className="tabs">
            {(['generate', 'model', 'dir', 'analyse'] as const).map((t) => (
              <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
            {injectionVisible && (
              <button
                className={`injection-tab${tab === 'injection' ? ' active' : ''}`}
                onClick={() => setTab('injection')}
                title="Values for the {{variables}} in the prompt, one row per generation">
                Injection
              </button>
            )}
          </nav>}
          {app === 'story' && <nav className="tabs">
            {(['generate', 'model', 'voices', 'player'] as const).map((t) => (
              <button key={t} className={storyTab === t ? 'active' : ''} onClick={() => setStoryTab(t)}>
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </nav>}
          <a className="help-btn" href={app === 'story' ? '/help#story' : '/help'} target="_blank" rel="noreferrer"
            title="Open docs (docs.html)">?</a>
        </div>
      </header>

      {app === 'image' && tab === 'generate' && (
        <section className="hero">
          <div className="hero-copy">
            <div className="mono-label">AI image generate</div>
            <h1>Describe it. Generate it. Keep iterating.</h1>
            <p>Create images from your home quickly and easily.</p>
          </div>
        </section>
      )}

      {notice && <div className="notice">{notice}</div>}

      {app === 'story' && <StoryApp notice={setNotice} tab={storyTab} setTab={setStoryTab} />}

      {app === 'image' && <>

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
          countText={countText} setCountText={setCountText}
          injectionCells={injectionCells} setInjectionCells={setInjectionCells}
          dynamicDirs={dynamicDirs}
          logSort={parseSort(cfg.log_sort)} setLogSort={(s) => set('log_sort', formatSort(s))}
        />
      )}
      {tab === 'injection' && injectionVisible && (
        <Injection
          prompt={prompt} countText={countText}
          cells={injectionCells} setCells={setInjectionCells}
        />
      )}
      {tab === 'model' && (
        <Model
          provider={cfg.provider} setProvider={(v) => set('provider', v)}
          model={cfg.model} setModel={(v) => set('model', v)}
          summaryModel={cfg.summary_model} setSummaryModel={(v) => set('summary_model', v)}
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
          countText={countText} dynamicDirs={dynamicDirs} setDynamicDirs={setDynamicDirs}
        />
      )}
      {tab === 'analyse' && (
        <Analyse
          outputDir={cfg.output_dir}
          folders={cfg.analyse ?? []} setFolders={(f) => set('analyse', f)}
          chosenDir={cfg.chosen_dir ?? ''} setChosenDir={(d) => set('chosen_dir', d)}
        />
      )}
      </>}
    </div>
    </>
  )
}
