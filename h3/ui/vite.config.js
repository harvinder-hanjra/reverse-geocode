import { defineConfig } from 'vite';

export default defineConfig({
  base: '/reverse-geocode/h3/',
  server: { port: 5176 },
  build: {
    rollupOptions: {
      output: { manualChunks: { maplibre: ['maplibre-gl'] } },
    },
  },
});
