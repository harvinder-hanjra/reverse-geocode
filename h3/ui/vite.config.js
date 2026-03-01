import { defineConfig } from 'vite';

export default defineConfig({
  server: { port: 5176 },
  build: {
    rollupOptions: {
      output: { manualChunks: { maplibre: ['maplibre-gl'] } },
    },
  },
});
