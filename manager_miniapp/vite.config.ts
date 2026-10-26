import { defineConfig } from 'vite';

export default defineConfig({
  base: '/manager/',
  server: { proxy: { '/manager/api': 'http://127.0.0.1:8000' } },
  build: { sourcemap: false, outDir: '../app/static/manager', emptyOutDir: true },
});
