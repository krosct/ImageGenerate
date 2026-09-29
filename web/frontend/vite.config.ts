import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { spawn, type ChildProcess } from 'node:child_process'
import { connect } from 'node:net'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

// The FastAPI backend must be running for /api/* (see the proxy below). In dev
// this plugin starts web/server.py itself, so `npm run dev` is enough; it is
// killed when Vite exits. Set IMAGE_GENERATE_PYTHON to use a venv interpreter.
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const BACKEND_HOST = '127.0.0.1'
const BACKEND_PORT = 8000
const BACKEND_SCRIPT = path.join(REPO_ROOT, 'web', 'server.py')
const PYTHON = process.env.IMAGE_GENERATE_PYTHON || 'python3'
const PROBE_TIMEOUT_MS = 500

function portInUse(port: number, host: string): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = connect({ port, host })
    const done = (used: boolean) => {
      socket.destroy()
      resolve(used)
    }
    socket.setTimeout(PROBE_TIMEOUT_MS)
    socket.once('connect', () => done(true))
    socket.once('timeout', () => done(false))
    socket.once('error', () => done(false))
  })
}

function backendPlugin(): Plugin {
  let child: ChildProcess | null = null

  const stop = () => {
    if (child === null) return
    const dying = child
    child = null
    dying.kill('SIGTERM')
  }

  return {
    name: 'imagegenerate-backend',
    apply: 'serve',
    async configureServer(server) {
      if (await portInUse(BACKEND_PORT, BACKEND_HOST)) {
        server.config.logger.info(
          `[backend] porta ${BACKEND_PORT} já em uso — usando o backend existente`,
        )
        return
      }
      child = spawn(PYTHON, [BACKEND_SCRIPT, '--port', String(BACKEND_PORT)], {
        cwd: REPO_ROOT,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      child.stdout?.on('data', (chunk: Buffer) => process.stdout.write(`[backend] ${chunk}`))
      child.stderr?.on('data', (chunk: Buffer) => process.stderr.write(`[backend] ${chunk}`))
      child.once('error', (err) => {
        child = null
        server.config.logger.error(`[backend] falha ao iniciar: ${err.message}`)
      })
      child.once('exit', (code, signal) => {
        child = null
        if (code !== 0 && signal === null) {
          server.config.logger.error(`[backend] encerrou com código ${code}`)
        }
      })
      server.httpServer?.once('close', stop)
      process.once('exit', stop)
    },
  }
}

export default defineConfig({
  plugins: [react(), backendPlugin()],
  server: {
    host: BACKEND_HOST,
    port: 5173,
    proxy: {
      '/api': `http://${BACKEND_HOST}:${BACKEND_PORT}`,
    },
  },
})
