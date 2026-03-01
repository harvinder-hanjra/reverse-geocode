import { defineConfig } from 'vite';

export default defineConfig({
  base: '/s2/',
  server: { port: 5175 },
  build: {
    rollupOptions: {
      output: { manualChunks: { maplibre: ['maplibre-gl'] } },
    },
  },
});
