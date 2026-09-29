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
  prompt?: string  // last prompt, shared with the desktop GUI (config.json)
  analyse?: string[]   // Analyse tab folders (shared with the GUI)
  chosen_dir?: string  // Analyse Choose target ('' = <parent of output dir>/chosen)
  log_sort?: string    // "column:asc|desc" of the history table
}

export interface AnalyseCell { path: string; name: string; size: number; modified: string; width: number; height: number }
export interface AnalyseRows {
  folders: { path: string; name: string; count: number }[]
  rows: (AnalyseCell | null)[][]
}
export interface ChooseResult { folder: string; report_md: string; report_csv: string; rows: Record<string, string>[] }

export interface StoryScene { number: number; heading: string; narration: string; start?: number; end?: number }
export interface StoryInfo {
  folder: string; label: string; title: string; style: string; language: string; logline: string
  scenes: StoryScene[]; voice_label: string; voice_id: string; voice_reason: string
  audio_seconds: number; image: string; audio: string; audio_deleted: boolean; scene_word: string
}
export interface StoryVoice { id: string; label: string; title?: string; languages?: string[]; tags?: string[]; description?: string }
export interface StoryConfig {
  input_dir: string; output_dir: string; writer_model: string; tts_model: string; language: string
  voices: StoryVoice[]; dry_run: boolean; force: boolean; style: string; duration: string; log_sort: string
}
export interface StoryMeta {
  tts_models: string[]; default_tts_model: string; writer_default_model: string; languages: string[]
  styles: { id: string; label: string; short: string }[]; default_style: string; max_voices: number
  log_fields: string[]; default_output_dir: string
}
export interface StoryLog { rows: Record<string, string>[]; fields: string[]; output_dir: string; home: string; failed: string[]; total_cost: number }
export interface StoryJobEvent {
  status: 'running' | 'done' | 'cancelled' | 'error'
  elapsed?: number; step?: string; step_seconds?: number; error?: string
  progress?: { done: number; total: number; failed: number }
  result?: { created: string[]; skipped: string[]; errors: { source: string; error: string }[]; cost: number; elapsed: number }
}

// Dynamic dirs: generations use <dir>/<start>..<dir>/<range>, `batch` of them
// per folder, cycling back to start (only when count > 1). Empty start/batch = 1.
export type DynamicKey = 'output_dir' | 'context_dir' | 'memory_dir'
export type DynamicDirs = Record<DynamicKey, { enabled: boolean; start: string; range: string; batch: string }>

export interface LogResponse {
  rows: Record<string, string>[]
  total_ops: number
  total_cost: number
  fields: string[]
}

export interface JobDone {
  status: 'done'
  result: { images: string[]; elapsed: number; cost: number; log_path: string; log_dirs: string[]; total_ops: number; total_cost: number }
}

export type JobEvent =
  | { status: 'running'; elapsed: number; progress?: { done: number; total: number; log_dirs: string[] } }
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
  // Rows come newest first; extraDirs adds a batch's Dynamic output logs.
  log: (outputDir: string, extraDirs: string[] = []) =>
    req<LogResponse>(`/api/log?output_dir=${encodeURIComponent(outputDir)}`
      + extraDirs.map((d) => `&extra_dir=${encodeURIComponent(d)}`).join('')),
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
    req<{ path: string; missing: boolean; parent: string; home: string; dirs: string[] }>(
      `/api/browse?path=${encodeURIComponent(path)}`),
  mkdir: (path: string, name: string) =>
    req<{ path: string }>('/api/browse/mkdir', { method: 'POST', body: JSON.stringify({ path, name }) }),
  // local files (server is localhost-only): images, thumbnails, audio, reports
  fileUrl: (path: string) => `/api/file?path=${encodeURIComponent(path)}`,
  thumbUrl: (path: string, size: number) => `/api/thumb?path=${encodeURIComponent(path)}&size=${size}`,
  open: (path: string) => req<{ opened: string }>('/api/open', { method: 'POST', body: JSON.stringify({ path }) }),
  // Analyse
  analyseRows: (folders: string[], newestFirst: boolean) =>
    req<AnalyseRows>('/api/analyse/rows?' + folders.map((f) => `folder=${encodeURIComponent(f)}`).join('&')
      + `&newest_first=${newestFirst}`),
  choose: (body: { folders: string[]; picks: Record<string, number>; chosen_dir: string; output_dir: string; newest_first: boolean }) =>
    req<ChooseResult>('/api/analyse/choose', { method: 'POST', body: JSON.stringify(body) }),
  defaultChosen: (outputDir: string) =>
    req<{ chosen_dir: string }>(`/api/analyse/default-chosen?output_dir=${encodeURIComponent(outputDir)}`),
  // StoryGenerate
  storyMeta: () => req<StoryMeta>('/api/story/meta'),
  storyConfig: () => req<StoryConfig>('/api/story/config'),
  saveStoryConfig: (cfg: Partial<StoryConfig>) =>
    req<StoryConfig>('/api/story/config', { method: 'PUT', body: JSON.stringify(cfg) }),
  checkVoices: (voices: StoryVoice[]) =>
    req<{ voices: StoryVoice[] }>('/api/story/voices/check', { method: 'POST', body: JSON.stringify({ voices }) }),
  storyLog: (outputDir: string, style: string) =>
    req<StoryLog>(`/api/story/log?output_dir=${encodeURIComponent(outputDir)}&style=${encodeURIComponent(style)}`),
  stories: (outputDir: string) =>
    req<{ stories: StoryInfo[] }>(`/api/story/stories?output_dir=${encodeURIComponent(outputDir)}`),
  storyGenerate: (body: Record<string, unknown>) =>
    req<{ job_id: string; total: number }>('/api/story/generate', { method: 'POST', body: JSON.stringify(body) }),
  storyDelete: (folder: string, audioOnly: boolean) =>
    req<{ method: string }>('/api/story/delete', { method: 'POST', body: JSON.stringify({ folder, audio_only: audioOnly }) }),
}

export function listenStoryJob(jobId: string, onEvent: (ev: StoryJobEvent) => void): () => void {
  const src = new EventSource(`/api/jobs/${jobId}/events`)
  src.onmessage = (msg) => {
    const ev = JSON.parse(msg.data) as StoryJobEvent
    onEvent(ev)
    if (ev.status !== 'running') src.close()
  }
  src.onerror = () => {
    onEvent({ status: 'error', error: 'lost connection to server' })
    src.close()
  }
  return () => src.close()
}

export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
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
