import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// `npm run dev` proxies the API and live socket to the FastAPI server on :8000.
// `npm run build` writes dist/, which FastAPI serves at http://localhost:8000.
export default defineConfig({
  plugins: [svelte()],
  // the History charts (ECharts) are their own chunk, loaded lazily; ~560 kB is expected for it
  build: { chunkSizeWarningLimit: 600 },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
});
