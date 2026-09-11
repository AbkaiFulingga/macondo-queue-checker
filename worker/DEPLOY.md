# Deploying the CORS proxy (one-time, ~5 minutes)

The static site is on GitHub Pages; Macondo's API sends no CORS headers, so
the site needs this tiny proxy to fetch *live* per-visitor project status.
**If you skip this, the site still works** — it falls back to last night's
snapshot for project status (clearly labeled), and all graphs/stats work.

## Steps

1. **Cloudflare account** (free): https://dash.cloudflare.com/sign-up

2. **Install wrangler once** (Node):
   ```bash
   npm install -g wrangler
   wrangler login   # opens browser, click Allow)
   ```

3. **Deploy from this directory**:
   ```bash
   cd worker
   wrangler deploy
   ```
   It prints your worker URL, e.g.
   `https://macondo-queue-proxy.<your-name>.workers.dev`

4. **Tell the site about it**: edit `site/app.js` — set
   ```js
   const PROXY = "https://macondo-queue-proxy.<your-name>.workers.dev";
   ```
   (or leave the placeholder and the site uses snapshot fallback).

## What it allows

Only three public GET endpoints:
- `/api/projects/{id}` — project record incl. live activeShip
- `/api/projects/{id}/ships` — ship history incl. decisions
- `/api/users/{username}` — a user's public projects

No auth headers are forwarded, so private data can never pass through it.
Free tier: 100k requests/day — far beyond what the site will use
(~2 requests per visitor lookup, cached 60s at the edge).

## Sanity check after deploy

```bash
curl https://macondo-queue-proxy.<your-name>.workers.dev/api/projects/906 | jq '.activeShip'
```

You should see ship 7520's status — through the proxy.
