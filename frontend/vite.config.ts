import {transformSync} from '@babel/core';
// @ts-expect-error — babel-plugin-relay has no bundled type defs
import relayPlugin from 'babel-plugin-relay';
import {tanstackRouter} from '@tanstack/router-plugin/vite';
import react from '@vitejs/plugin-react';
import {defineConfig, type Plugin} from 'vite';

// @vitejs/plugin-react v6 dropped Babel, so we run babel-plugin-relay
// ourselves on .ts/.tsx files that contain a `graphql` tagged template.
function relayTransform(): Plugin {
  return {
    name: 'relay-transform',
    enforce: 'pre',
    transform(code, id) {
      if (!/\.(t|j)sx?$/.test(id)) return null;
      if (id.includes('node_modules') || id.includes('__generated__')) return null;
      if (!code.includes('graphql`')) return null;
      const result = transformSync(code, {
        plugins: [[relayPlugin, {artifactDirectory: './src/__generated__'}]],
        babelrc: false,
        configFile: false,
        filename: id,
        sourceMaps: true,
        parserOpts: {plugins: ['typescript', 'jsx']},
      });
      if (!result?.code) return null;
      // Serialize rather than hand the object over: babel types the map's
      // `names`/`sources` as readonly arrays, while rollup's ExistingRawSourceMap
      // wants mutable ones, so the object form doesn't typecheck. `SourceMapInput`
      // also accepts a raw sourcemap JSON string, which carries identical data.
      return {code: result.code, map: result.map ? JSON.stringify(result.map) : undefined};
    },
  };
}

export default defineConfig({
  plugins: [
    tanstackRouter({routesDirectory: './src/routes'}),
    relayTransform(),
    react(),
  ],
  server: {
    proxy: {
      '/graphql': {target: 'http://localhost:8000', ws: true, changeOrigin: true},
      '/uploads': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/ws/live': {target: 'ws://localhost:8000', ws: true},
      '/tts': 'http://localhost:8000',
      '/transcribe': 'http://localhost:8000',
      '/artifacts': 'http://localhost:8000',
      '/server-logs': 'http://localhost:8000',
    },
  },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
});
