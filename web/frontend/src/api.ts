export interface Provider {
  id: string
  label: string
  env_var: string
  default_model: string
}

export interface ProvidersResponse {
  providers: Provider[]
  default_provider: string
  aspect_ratios: string[]
  resolutions: string[]
  output_formats: string[]
}

export interface AppConfig {
  output_dir: string
  context_dir: string
  memory_dir: string
  provider: string
  model: string
  summary_model: string
  prop: string
  resolution: string
  output_format: string
  dry_run: boolean
}

export interface LogResponse {
  rows: Record<string, string>[]
  total_ops: number
  total_cost: number
  fields: string[]
}

export interface JobDone {
  status: 'done'
  result: { images: string[]; elapsed: number; cost: number; log_path: string; total_ops: number; total_cost: number }
}

export type JobEvent =
  | { status: 'running'; elapsed: number }
  | JobDone
  | { status: 'cancelled' }
  | { status: 'error'; error: string }

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status}: ${text.slice(0, 300)}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  providers: () => req<ProvidersResponse>('/api/providers'),
  config: () => req<AppConfig>('/api/config'),
  saveConfig: (cfg: Partial<AppConfig>) =>
    req<AppConfig>('/api/config', { method: 'PUT', body: JSON.stringify(cfg) }),
  log: (outputDir: string) =>
    req<LogResponse>(`/api/log?output_dir=${encodeURIComponent(outputDir)}`),
  keys: () => req<Record<string, { configured: boolean; source: string | null; vault_dir: string }>>('/api/keys'),
  rememberKey: (provider: string, apiKey: string) =>
    req<{ saved: string }>(`/api/keys/${provider}`, { method: 'PUT', body: JSON.stringify({ api_key: apiKey }) }),
  forgetKey: (provider: string) =>
    req<{ forgotten: boolean }>(`/api/keys/${provider}`, { method: 'DELETE' }),
  generate: (body: Record<string, unknown>) =>
    req<{ job_id: string; key_source: string }>('/api/generate', { method: 'POST', body: JSON.stringify(body) }),
  cancel: (jobId: string) =>
    req<{ cancelled: boolean }>(`/api/jobs/${jobId}/cancel`, { method: 'POST' }),
  imageUrl: (outputDir: string, name: string) =>
    `/api/images?output_dir=${encodeURIComponent(outputDir)}&name=${encodeURIComponent(name)}`,
  browse: (path: string) =>
    req<{ path: string; parent: string; home: string; dirs: string[] }>(
      `/api/browse?path=${encodeURIComponent(path)}`),
  mkdir: (path: string, name: string) =>
    req<{ path: string }>('/api/browse/mkdir', { method: 'POST', body: JSON.stringify({ path, name }) }),
}

export function listenJob(jobId: string, onEvent: (ev: JobEvent) => void): () => void {
  const src = new EventSource(`/api/jobs/${jobId}/events`)
  src.onmessage = (msg) => {
    const ev = JSON.parse(msg.data) as JobEvent
    onEvent(ev)
    if (ev.status !== 'running') src.close()
  }
  src.onerror = () => {
    onEvent({ status: 'error', error: 'lost connection to server' })
    src.close()
  }
  return () => src.close()
}
