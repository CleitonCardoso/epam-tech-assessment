import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The built bundle only ever calls same-origin relative `/api/...` URLs (nginx proxies them
// in production). The dev-server proxy below is a local convenience only: it never ends up
// in `dist/`.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
