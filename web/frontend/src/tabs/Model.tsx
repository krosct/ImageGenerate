import { useEffect, useState } from 'react'
import { api, Provider } from '../api'

interface Props {
  provider: string
  setProvider: (v: string) => void
  model: string
  setModel: (v: string) => void
  summaryModel: string
  setSummaryModel: (v: string) => void
  apiKey: string
  setApiKey: (v: string) => void
  rememberKey: boolean
  setRememberKey: (v: boolean) => void
  providers: Provider[]
  status: (msg: string) => void
}

export default function Model(p: Props) {
  const [keys, setKeys] = useState<Record<string, { configured: boolean; source: string | null; vault_dir: string }>>({})

  async function reloadKeys() {
    try { setKeys(await api.keys()) } catch { /* offline */ }
  }
  useEffect(() => { void reloadKeys() }, [])

  const current = keys[p.provider]

  async function onForget() {
    try {
      const res = await api.forgetKey(p.provider)
      p.status(res.forgotten ? `forgot remembered key for ${p.provider}` : `no remembered key for ${p.provider}`)
      if (res.forgotten) p.setRememberKey(false)
      await reloadKeys()
    } catch (e) {
      p.status(`forget error: ${(e as Error).message}`)
    }
  }

  return (
    <div className="card">
      <label>Provider</label>
      <select value={p.provider} onChange={(e) => {
        const next = e.target.value
        p.setProvider(next)
        const info = p.providers.find((x) => x.id === next)
        if (info && !p.model) p.setModel(info.default_model)
      }}>
        {p.providers.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
      </select>

      <label>Model</label>
      <input type="text" value={p.model} onChange={(e) => p.setModel(e.target.value)}
        placeholder={p.providers.find((x) => x.id === p.provider)?.default_model} />

      <label title="Chat model that writes the 1-sentence log summary. Tip: use a free or small model (e.g. openrouter/free) so summaries cost nothing. Required: generation will not start with this field empty.">
        Summary model ⓘ</label>
      <input type="text" value={p.summaryModel} onChange={(e) => p.setSummaryModel(e.target.value)}
        placeholder="openrouter/free" />

      <label>API key {current?.configured ? `(configured via ${current.source})` : '(not configured)'}</label>
      <div className="row">
        <div style={{ flex: 3 }}>
          <input type="password" value={p.apiKey} onChange={(e) => p.setApiKey(e.target.value)} />
        </div>
        <div>
          <label title={`Encrypt and save this key in ${current?.vault_dir ?? ''}/<provider>_api_key.enc so you don't type it again.`}>
            <input type="checkbox" checked={p.rememberKey}
              onChange={(e) => p.setRememberKey(e.target.checked)} /> remember me</label>
        </div>
        <div>
          <button className="ghost" onClick={onForget}
            title={`Delete the saved key and its local secret (<provider>_api_key.enc and <provider>.fkey) from ${current?.vault_dir ?? ''}/.`}>
            Forget</button>
        </div>
      </div>
      {current && <div className="hint">vault: {current.vault_dir}</div>}
    </div>
  )
}
