import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Marketing + app site for Reverse ATS — deploys to Cloudflare Pages.
// API endpoints are read from VITE_API_URL (defaults to live Worker).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: 5174, // 5173 is the existing local app; 5174 keeps them parallel-runnable
  },
})
