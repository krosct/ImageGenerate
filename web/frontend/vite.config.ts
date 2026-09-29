import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { spawn, type ChildProcess } from 'node:child_process'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

// The FastAPI backend must be running for /api/* and /help (see the proxy below).
// In dev this plugin starts web/server.py itself, so `npm run dev` is enough; it
// is killed when Vite exits. Port 8000 is only reused when an ImageGenerate
// backend already answers there — another app may own that port (e.g. a Laravel
// server), so in that case the first free port from 8000 up is used instead.
// Set IMAGE_GENERATE_PYTHON to use a venv interpreter.
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const BACKEND_HOST = '127.0.0.1'
const BASE_PORT = 8000
const MAX_PORT_TRIES = 20
const BACKEND_SCRIPT = path.join(REPO_ROOT, 'web', 'server.py')
const PYTHON = process.env.IMAGE_GENERATE_PYTHON || 'python3'
const PROBE_TIMEOUT_MS = 500
const HEALTH_PATH = '/api/providers'
const PROXY_PREFIXES = ['/api', '/help']

// True only for OUR backend: it answers /api/providers with a JSON object that
// has a `providers` array. A foreign server on the port returns false.
async function isOurBackend(port: number): Promise<boolean> {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS)
    const res = await fetch(`http://${BACKEND_HOST}:${port}${HEALTH_PATH}`,
      { signal: controller.signal })
    clearTimeout(timer)
    if (!res.ok) return false
    const data = (await res.json()) as { providers?: unknown }
    return Array.isArray(data.providers)
  } catch {
    return false
  }
}

function freePortFrom(start: number, host: string): Promise<number> {
  return new Promise((resolve, reject) => {
    let port = start
    const attempt = () => {
      if (port > start + MAX_PORT_TRIES) {
        reject(new Error(`no free port in ${start}..${start + MAX_PORT_TRIES}`))
        return
      }
      const probe = createServer()
      probe.unref()
      probe.once('error', () => { port += 1; attempt() })
      probe.once('listening', () => probe.close(() => resolve(port)))
      probe.listen(port, host)
    }
    attempt()
  })
}

function backendPlugin(): Plugin {
  let child: ChildProcess | null = null
  let backendPort = BASE_PORT

  const stop = () => {
    if (child === null) return
    const dying = child
    child = null
    dying.kill('SIGTERM')
  }

  const startBackend = (port: number) => {
    child = spawn(PYTHON, [BACKEND_SCRIPT, '--port', String(port)], {
      cwd: REPO_ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    child.stdout?.on('data', (chunk: Buffer) => process.stdout.write(`[backend] ${chunk}`))
    child.stderr?.on('data', (chunk: Buffer) => process.stderr.write(`[backend] ${chunk}`))
    child.once('error', (err) => {
      child = null
      console.error(`[backend] falha ao iniciar: ${err.message}`)
    })
    child.once('exit', (code, signal) => {
      child = null
      if (code !== 0 && signal === null) {
        console.error(`[backend] encerrou com código ${code}`)
      }
    })
  }

  return {
    name: 'imagegenerate-backend',
    apply: 'serve',
    // Runs before the dev server (and its proxy) is created, so the proxy can
    // point at the port we actually picked.
    async config() {
      if (await isOurBackend(BASE_PORT)) {
        backendPort = BASE_PORT
        console.log(`[backend] reusing ImageGenerate backend on ${BACKEND_HOST}:${backendPort}`)
      } else {
        backendPort = await freePortFrom(BASE_PORT, BACKEND_HOST)
        if (backendPort !== BASE_PORT) {
          console.log(`[backend] porta ${BASE_PORT} ocupada por outro app; usando ${backendPort}`)
        }
        startBackend(backendPort)
        console.log(`[backend] starting web/server.py on ${BACKEND_HOST}:${backendPort}`)
      }
      process.once('exit', stop)
      return {
        server: {
          proxy: Object.fromEntries(
            PROXY_PREFIXES.map((prefix) => [prefix, `http://${BACKEND_HOST}:${backendPort}`]),
          ),
        },
      }
    },
    configureServer(server) {
      server.httpServer?.once('close', stop)
    },
  }
}

export default defineConfig({
  plugins: [react(), backendPlugin()],
  server: {
    host: BACKEND_HOST,
    port: 5173,
  },
})
