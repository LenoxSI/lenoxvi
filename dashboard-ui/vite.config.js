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
    // dev server proxies API calls to the live bot on the appliance
    proxy: {
      '/api': {
        target: 'https://192.0.2.185:7860',
        changeOrigin: true,
        secure: false, // self-signed cert on the appliance
      },
    },
  },
})