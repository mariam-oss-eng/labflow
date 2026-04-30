# Progressive Web App (v0.9)

LabFlow ships an installable PWA: a manifest, a service worker, and an
offline shell — no build step required.

## Files

| Path | Purpose |
|---|---|
| `/static/manifest.webmanifest` | Web app manifest (name, theme, icons) |
| `/static/sw.js`                | Service worker (cache + offline fallback) |
| `/static/offline.html`         | The cached page shown when offline |
| `/static/icon-192.svg`, `/static/icon-512.svg` | Maskable icons |

## Caching strategy

* **Static assets** (`/static/*`) — cache-first. Repeat loads are
  instant after the first visit.
* **HTML / JSON** — network-first with offline fallback. Fresh data
  when online, the cached shell when offline.
* **Cache name** — `labflow-v1`. Bump in `sw.js` when you ship breaking
  asset changes.

## Wire it up in your shell template

```html
<link rel="manifest" href="/static/manifest.webmanifest">
<meta name="theme-color" content="#0ea5e9">
<script>
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/sw.js");
  }
</script>
```

Browsers that meet the PWA criteria (HTTPS, manifest, registered SW)
will offer "Install LabFlow" automatically. iOS Safari requires the
user to use Share → Add to Home Screen.

## Limitations

* The current SW caches the offline shell only; full offline editing of
  meetings is not in scope for v0.9 (no background sync queue yet).
* The PWA shell uses the same auth cookie / API key as the regular
  site — there's no separate offline credential store.
