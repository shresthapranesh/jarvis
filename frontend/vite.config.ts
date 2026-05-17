import {tanstackRouter} from '@tanstack/router-plugin/vite';
import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';

export default defineConfig({
  plugins: [tanstackRouter({routesDirectory: './src/routes'}), react()],
  server: {
    proxy: {
      '/run': 'http://localhost:8000',
      '/conversations': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/automations': 'http://localhost:8000',
      '/stream': 'http://localhost:8000',
      '/stop': 'http://localhost:8000',
      '/resume': 'http://localhost:8000',
      '/ws/live': {target: 'ws://localhost:8000', ws: true},
      '/tts': 'http://localhost:8000',
      '/transcribe': 'http://localhost:8000',
      '/workflows': 'http://localhost:8000',
      '/workflow-runs': 'http://localhost:8000',
      '/artifacts': 'http://localhost:8000',
      '/documents': 'http://localhost:8000',
      '/task-runs': 'http://localhost:8000',
      '/agent-memory': 'http://localhost:8000',
      '/consolidate-memory': 'http://localhost:8000',
      '/server-logs': 'http://localhost:8000',
      '/notification-channels': 'http://localhost:8000',
    },
  },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
});
