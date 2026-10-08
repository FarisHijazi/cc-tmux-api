// Only this explicit public shell allowlist is cached. API/transcript requests
// always pass through the network, including when a device is offline.
const CACHE = 'hq-shell-v2';
const SHELL = ['/', '/cc-static/app.js', '/cc-static/styles.css', '/cc-static/manifest.webmanifest', '/cc-static/icons/hq.svg', '/cc-static/icons/hq-192.png', '/cc-static/icons/hq-512.png', '/cc-static/icons/apple-touch-icon.png', '/cc-static/vendor/preact.mjs', '/cc-static/vendor/hooks.mjs', '/cc-static/vendor/htm.mjs', '/cc-static/vendor/marked.mjs', '/cc-static/vendor/dompurify.mjs'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('hq-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.search || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && !response.redirected) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(event.request, copy)));
    }
    return response;
  }).catch(async () => {
    const cached = await caches.match(event.request);
    return cached || new Response('HQ is offline. Reconnect and reload.', { status:503, headers:{ 'Content-Type':'text/plain' } });
  }));
});
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data?.json() || {}; } catch { data = { body:event.data?.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || 'HQ needs your attention', {
    body:data.body || 'Open your session to continue.', icon:'/cc-static/icons/hq.svg',
    tag:data.tag || data.session_id || 'hq', data:{ session_id:data.session_id || data.id || '', url:data.url || '/' },
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const payload = event.notification.data || {};
  const supplied = new URL(payload.url || '/', self.location.origin);
  const id = payload.session_id || (supplied.origin === self.location.origin ? new URLSearchParams(supplied.hash.slice(1)).get('session') : null);
  const url = new URL(id ? `/#session=${encodeURIComponent(id)}` : '/', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type:'window', includeUncontrolled:true }).then(async clients => {
    const client = clients.find(client => new URL(client.url).origin === self.location.origin);
    if (client) { await client.navigate(url); return client.focus(); }
    return self.clients.openWindow(url);
  }));
});
