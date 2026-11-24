import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  build: { outDir: '../src/regresslens/web', emptyOutDir: true, chunkSizeWarningLimit: 850 },
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
});
