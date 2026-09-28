import express from 'express';
import { createProxyMiddleware } from 'http-proxy-middleware';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = 7777;

// Proxy API requests to backend
app.use('/api', createProxyMiddleware({
  target: 'http://localhost:9050',
  changeOrigin: true,
  onError: (err, req, res) => {
    console.error('Proxy error:', err);
    res.status(500).send('Proxy error');
  }
}));

// Proxy WebSocket requests
app.use('/ws', createProxyMiddleware({
  target: 'ws://localhost:9050',
  ws: true,
  changeOrigin: true
}));

// Serve static files
app.use(express.static(path.join(__dirname, 'dist')));

// Handle client-side routing - serve index.html for all routes
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'dist', 'index.html'));
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Server running at http://0.0.0.0:${PORT}`);
  console.log(`Proxying /api requests to http://localhost:9050`);
});