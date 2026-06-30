import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Backend URL is configurable via VITE_API_URL in .env / .env.local.
// Defaults to http://localhost:8091 — matching the README setup steps —
// so a fresh `git clone && npm install && npm run dev` just works against
// a backend started on the same machine.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiUrl = env.VITE_API_URL || 'http://localhost:8091'
  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        '/api': apiUrl,
        '/health': apiUrl,
      },
    },
  }
})
