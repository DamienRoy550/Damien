/**
 * Minimal static file server for the exported web demo (no dependencies).
 * Serves ./webapp with SPA fallback to index.html.
 *
 * Usage: node scripts/serve-demo.js [port]
 */
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', 'webapp');
const PORT = Number(process.argv[2] || process.env.PORT || 4173);

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
  '.map': 'application/json',
  '.txt': 'text/plain; charset=utf-8',
};

const server = http.createServer((req, res) => {
  try {
    const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
    let filePath = path.normalize(path.join(ROOT, urlPath));

    // stay inside webapp/
    if (!filePath.startsWith(ROOT)) {
      res.writeHead(403);
      res.end('Forbidden');
      return;
    }

    if (urlPath === '/' || !path.extname(filePath)) {
      filePath = path.join(ROOT, 'index.html'); // SPA fallback
    }

    fs.readFile(filePath, (err, data) => {
      if (err) {
        // final fallback: serve index.html for unknown routes
        fs.readFile(path.join(ROOT, 'index.html'), (err2, index) => {
          if (err2) {
            res.writeHead(404);
            res.end('Not found (did you build the demo? `npm run export:web`)');
            return;
          }
          res.writeHead(200, { 'Content-Type': MIME['.html'] });
          res.end(index);
        });
        return;
      }
      const ext = path.extname(filePath).toLowerCase();
      res.writeHead(200, {
        'Content-Type': MIME[ext] || 'application/octet-stream',
        'Cache-Control': 'no-cache',
      });
      res.end(data);
    });
  } catch {
    res.writeHead(500);
    res.end('Server error');
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Damien web demo: http://localhost:${PORT} (serving ${ROOT})`);
});
