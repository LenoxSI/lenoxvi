import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// lenoxvi dashboard build. Prebuilt dist/ is committed — a fresh clone
// runs with zero node toolchain (public-flip rule); this build stays
// dev-side. Base path is relative so the page works under any host
// mount (FastAPI serves it at /dashboard).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    // dev server proxies API calls to a running bot. Point it at yours:
    //   LENOXVI_DEV_BOT_URL=https://<bot-host>:7860 npx vite
    proxy: {
      '/api': {
        target: process.env.LENOXVI_DEV_BOT_URL || 'https://localhost:7860',
        changeOrigin: true,
        secure: false, // self-signed cert on the appliance
      },
    },
  },
})