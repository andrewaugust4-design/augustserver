# augustserver.com — Claude Code working notes

## You are running ON the production server
This repo (`~/augustserver`) is the **source of truth** for augustserver.com. You are
Claude Code running directly on the live Ubuntu box.

- **Edit files in this repo only.** Do NOT edit `/opt/<tool>/`, `/var/www/augustserver/`,
  `/etc/nginx/`, or `/etc/systemd/` directly — those are the live copies.
- **Deploying is a separate, human-run step.** After changing code, commit and push;
  Andrew then runs `./deploy.sh <target>` to sync to live paths and restart services.
  Do not run deploy.sh yourself unless asked (it needs sudo).
- **Commit and push as you work.** After a change: git add, commit with a clear message,
  and push to origin (github.com/andrewaugust4-design/augustserver).

## What it is
Self-hosted site + web tools. Stack: Ubuntu, nginx, Cloudflare in front, FastAPI apps
behind the proxy, php-fpm (for PrivateBin). Homepage is a single-page app ("Gradient
Glass" look) served from /var/www/augustserver/index.html — mirrored here as
homepage/index.html.

## Repo layout / where things deploy to
- homepage/index.html     -> /var/www/augustserver/index.html   (static; no restart)
- <tool>/app/             -> /opt/<tool>/app/                   (FastAPI; restart service)
- <tool>/requirements.txt, .env.example, deploy/  (reference)
- nginx-sites-available/  reference copies of /etc/nginx/sites-available/
NOT in the repo (gitignored, machine-specific): venv/, data/, .env, .cache/

## Homepage navigation structure
The homepage SPA has two top-level hub pages reached from the sidebar (`#tools`,
`#gaming`) plus `#home`. Individual tools/sections are NOT listed directly in the
sidebar or on the home page anymore — they're cards inside their hub's section.
- **Tools hub** (`section-tools`, icon-blue "Tools" nav item): card grid of the
  FastAPI tools below. Each card is a plain `<a href="/<tool>/">` (no data-section)
  so the SPA router leaves it alone.
- **General Gaming hub** (`section-gaming`, icon-green "General Gaming" nav item):
  card grid linking to in-SPA sections `status` (Server Status) and `cs2` (CS2
  Settings) via `data-section` home-cards.
- Sub-sections reached only through a hub (status, cs2) get a `.back-link` at the
  top pointing back to their hub (`data-section="gaming"`), and each hub section
  gets a `.back-link` back to `home`. The JS `navGroup` map keeps the parent hub's
  sidebar nav item highlighted while viewing one of its sub-sections.
- Home page itself now just has two home-cards: "Tools" and "General Gaming".

## The tools (each has its own port, color, "‹ Back to August." link)
| Tool        | Path          | Port | Color  |
|-------------|---------------|------|--------|
| musicreview | /musicreview/ | 8060 | amber  |
| paste       | /paste/       | —    | teal   |  (PrivateBin/PHP — not in this repo)
| imagetools  | /imagetools/  | 8061 | rose   |
| drop        | /drop/        | 8062 | orange |
| convert     | /convert/     | 8063 | lime   |
| video       | /video/       | 8064 | red    |
Next free port: 8065. Card color order inside the Tools hub: amber→teal→rose→orange→lime→red.
Top-level sidebar icons: purple (Home) → blue (Tools) → green (General Gaming).

## Conventions for a NEW tool
1. FastAPI in app/main.py, UI in app/static/index.html, plus __init__.py,
   requirements.txt, .env.example, deploy/ (nginx snippet + systemd unit).
2. Bind 127.0.0.1 on the next free port, run with --root-path /<tool>.
3. systemd: User=www-data, EnvironmentFile=/opt/<tool>/.env, ReadWritePaths=/opt/<tool>/data
   if it writes files.
4. nginx: `location = /<tool>` 301s to `/<tool>/`, plus a `location /<tool>/` proxy block;
   client_max_body_size = upload cap; generous proxy timeouts.
5. Frontend fetch paths are RELATIVE (api/...) so the page works at / and /<tool>/.
6. Long/CPU work uses the async job-queue + polling pattern, max_workers=1.
7. Add a home-card inside `section-tools` on the homepage (do NOT add a sidebar nav
   item or a top-level home-card — tools live only inside the Tools hub). Tool links
   are plain <a> WITHOUT data-section so the SPA router leaves them alone.

## Gotchas
- Model-cache perms: any tool whose ML lib downloads a model must point its cache to
  /opt/<tool>/.cache (HF_HOME / U2NET_HOME) owned by www-data, or first run fails.
- Cloudflare caches static files — hard-refresh / purge after a homepage deploy.
- yt-dlp rots: convert & video break when YouTube changes; fix with
  `pip install -U yt-dlp` in that tool's venv + restart.
- nginx configs are symlinked from sites-enabled; grep sites-available or use `nginx -T`.
- Diagnostics: systemctl status <tool>, journalctl -u <tool> -f, nginx -T, df -h, free -h.
