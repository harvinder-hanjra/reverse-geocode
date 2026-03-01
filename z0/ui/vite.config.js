import { defineConfig } from 'vite';

export default defineConfig({
  base: '/z0/',
  server: { port: 5174 },
  build: {
    rollupOptions: {
      output: { manualChunks: { maplibre: ['maplibre-gl'] } },
    },
  },
});
