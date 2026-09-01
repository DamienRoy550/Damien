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

    const hasExtension = Boolean(path.extname(filePath));
    if (urlPath === '/' || !hasExtension) {
      filePath = path.join(ROOT, 'index.html'); // SPA fallback
    }

    fs.readFile(filePath, (err, data) => {
      if (err) {
        // Missing ASSET (has extension) must 404 loudly — serving HTML here
        // makes the browser abort with a MIME error (blank white page).
        if (hasExtension) {
          res.writeHead(404, { 'Content-Type': 'text/plain' });
          res.end(`404: ${urlPath} not found — rebuild with \`npm run export:web\``);
          return;
        }
        // Unknown route → SPA
        fs.readFile(path.join(ROOT, 'index.html'), (err2, index) => {
          if (err2) {
            res.writeHead(404);
            res.end('Not found (did you build the demo? `npm run export:web`)');
            return;
          }
          res.writeHead(200, { 'Content-Type': MIME['.html'], 'Cache-Control': 'no-store' });
          res.end(injectErrorTrap(index));
        });
        return;
      }
      const ext = path.extname(filePath).toLowerCase();
      const headers = {
        'Content-Type': MIME[ext] || 'application/octet-stream',
        'Cache-Control': 'no-cache',
      };
      if (ext === '.html') {
        headers['Cache-Control'] = 'no-store';
        data = injectErrorTrap(data);
      }
      res.writeHead(200, headers);
      res.end(data);
    });
  } catch {
    res.writeHead(500);
    res.end('Server error');
  }
});

const TRAP = `<script>
(function(){
  function show(kind,msg){
    try{
      var el=document.createElement('pre');
      el.style.cssText='position:fixed;left:0;right:0;bottom:0;z-index:99999;margin:0;background:#7f1d1d;color:#fff;font:12px/1.5 Menlo,monospace;padding:10px;white-space:pre-wrap;max-height:45vh;overflow:auto';
      el.textContent=kind+': '+msg;
      (document.body||document.documentElement).appendChild(el);
    }catch(e){}
  }
  window.addEventListener('error',function(e){
    show('JS ERROR',(e.message||'unknown')+'  ['+String(e.filename||'').split('/').pop()+':'+e.lineno+':'+e.colno+']');
  });
  window.addEventListener('unhandledrejection',function(e){
    show('PROMISE REJECTION', String(e.reason && (e.reason.stack||e.reason.message) || e.reason));
  });
})();
</script>`;

function injectErrorTrap(html) {
  const s = html.toString('utf8');
  if (s.includes('JS ERROR')) return Buffer.from(s); // already injected
  return Buffer.from(s.replace('</head>', `${TRAP}\n</head>`));
}

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Damien web demo: http://localhost:${PORT} (serving ${ROOT})`);
});
