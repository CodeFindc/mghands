import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes, req, res) => {
            if (req.url && req.url.includes('/stream')) {
              res.setHeader('Cache-Control', 'no-cache, no-transform');
              res.setHeader('Connection', 'keep-alive');
              res.setHeader('X-Accel-Buffering', 'no');
            }
          });
        },
      },
      '/health': 'http://127.0.0.1:8080',
    },
  },
});
