import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { createLogger, type LogErrorOptions } from 'vite'

const logger = createLogger()
const logError = logger.error.bind(logger)

function isExpectedWsProxyAbort(msg: string, options?: LogErrorOptions): boolean {
  const code = (options?.error as NodeJS.ErrnoException | undefined)?.code
  return (
    (code === 'ECONNABORTED' || code === 'ECONNRESET') &&
    (msg.includes('ws proxy error:') || msg.includes('ws proxy socket error:'))
  )
}

logger.error = (msg, options) => {
  if (isExpectedWsProxyAbort(msg, options)) return
  logError(msg, options)
}

// https://vite.dev/config/
export default defineConfig({
  customLogger: logger,
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
      '/data': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
})
