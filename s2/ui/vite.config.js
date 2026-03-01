import { defineConfig } from 'vite';

export default defineConfig({
  server: { port: 5175 },
  build: {
    rollupOptions: {
      output: { manualChunks: { maplibre: ['maplibre-gl'] } },
    },
  },
});
