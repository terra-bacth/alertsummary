import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The dashboard can be pointed at any tsagent instance. By default the dev server proxies
// `/api` to the local backend so `npm run dev` works out of the box; set
// `VITE_TSAGENT_API` (e.g. https://your-route.apps.example.com) to hit a remote Route instead.
export default defineConfig({
  plugins: [react()],
  // Relative asset URLs, so the built dashboard works both at a Route's root and under a path
  // prefix (route.path + TSAGENT_ROOT_PATH) without rebuilding.
  base: './',
  server: {
    port: 5173,
    host: true,
    // The preview host is a per-sandbox wildcard subdomain; accept any host so the live
    // preview proxy can reach the dev server.
    allowedHosts: true,
    proxy: {
      '/api': {
        target: process.env.TSAGENT_BACKEND ?? 'http://localhost:8080',
        changeOrigin: true,
      },
      // Everything else the UI needs is already under /api.
    },
  },
});
