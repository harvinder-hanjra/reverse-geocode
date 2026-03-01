import { defineConfig } from 'vite';

export default defineConfig({
  base: '/reverse-geocode/z0/',
  server: { port: 5174 },
  build: {
    rollupOptions: {
      output: { manualChunks: { maplibre: ['maplibre-gl'] } },
    },
  },
});
