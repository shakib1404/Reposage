# Deploying RepoSage

RepoSage clones arbitrary public repositories, installs their dependencies and
**runs their code**. That single fact drives every decision below: it rules out
serverless platforms entirely, and it makes the security section non-optional.

---

## 1. Where this can run

| Platform | Works? | Why |
|---|---|---|
| **Azure VM** (Azure for Students) | ✅ recommended | $100 credit / 12 months, no credit card. See §2 |
| **Oracle Cloud Always Free** | ✅ best long-term | 4 ARM cores / 24GB **free forever**; signup capacity can be flaky |
| Hetzner / Linode / any VPS | ✅ | ~€4-6/mo for the same specs |
| DigitalOcean droplet | ✅ | Fine on a 2GB droplet (~$12/mo). Check the student credit first — it has been $200 and $5 at different times. See §2C |
| Fly.io / Railway | ⚠️ | Works, but needs a paid tier for enough RAM |
| Render free tier | ❌ | 512MB RAM — torch alone won't fit |
| Vercel / Netlify / Cloudflare Workers / Lambda | ❌ | No subprocess, no persistent FS, request timeouts far below a 200s execution, and SSE doesn't survive |

On Azure, the VM is the right service. App Service and Container Apps look
cheaper but sandbox the container, cap request duration well under a 200s
execution, and don't give the subprocess/filesystem freedom the executor needs.

**Sizing:** 2 vCPU / 4GB RAM / 80GB disk is the comfortable minimum. The backend
image is ~3.3GB, the reranker model holds ~500MB resident, and each concurrent
repo execution builds its own throwaway venv. 2GB works for light single-user
use — measured peak is 621MB during a search, so it fits with room to spare.

---

## 2. Azure, step by step (GitHub Student — $100 / 12 months)

**Claim the credit:** `azure.microsoft.com/free/students` → sign in with the
account linked to your GitHub Student pack. $100, 12 months, no card required.

**Pick a size.** B-series VMs are *burstable* — they bank CPU credits while
idle and spend them on bursts, which fits this workload well (long idle
stretches, then a heavy 200s execution).

| Size | vCPU / RAM | ~cost/mo | $100 runs for | Verdict |
|---|---|---|---|---|
| B1s | 1 / 1GB | ~$8 | ~12 mo | ❌ torch won't fit |
| **B1ms** | 1 / 2GB | ~$15 | **~6 mo** | ✅ tight but works — use §4 |
| **B2s** | 2 / 4GB | ~$30 | ~3 mo | ✅ comfortable |

Prices vary by region — check the Azure pricing calculator for yours.

**Create it** (portal: *Virtual machines → Create*, or CLI):

```bash
az login
az group create --name reposage-rg --location eastus

az vm create \
  --resource-group reposage-rg \
  --name reposage-vm \
  --image Ubuntu2404 \
  --size Standard_B1ms \
  --admin-username azureuser \
  --generate-ssh-keys \
  --storage-sku StandardSSD_LRS \
  --os-disk-size-gb 64

az vm open-port --resource-group reposage-rg --name reposage-vm --port 80  --priority 1001
az vm open-port --resource-group reposage-rg --name reposage-vm --port 443 --priority 1002
```

Two cost details that matter: pick **StandardSSD_LRS**, not Premium (Premium
can add $10-20/mo on its own), and 64GB of disk is plenty.

Then `ssh azureuser@<public-ip>` and continue from §3.

### Making $100 last the full 12 months

Azure bills compute only while a VM is **deallocated-vs-running**, so a
portfolio app that's only up for demos costs a fraction of the sticker price.
Note that shutting down from inside the OS does *not* stop billing — you must
deallocate:

```bash
az vm deallocate --resource-group reposage-rg --name reposage-vm   # stops billing
az vm start      --resource-group reposage-rg --name reposage-vm   # ~40s to come back
```

Or set a nightly auto-shutdown (portal: *VM → Auto-shutdown*), which roughly
halves the burn:

```bash
az vm auto-shutdown --resource-group reposage-rg --name reposage-vm --time 2000
```

With `restart: unless-stopped` in `docker-compose.yml`, the stack comes back on
its own after a start — nothing to re-run manually.

> The public IP changes on each deallocate/start unless you attach a **static**
> IP. If you're using a domain with HTTPS, make the IP static or the DNS record
> goes stale and the cert stops renewing.

---

## 2C. DigitalOcean, step by step

**Check your credit first.** The GitHub Student pack's DigitalOcean offer has
been $200/12 months at times and $5 at others. $200 comfortably covers a 2GB
droplet for the year; $5 buys about twelve days of one. Look at
`education.github.com/pack` before paying out of pocket — and compare with
Azure for Students (§2), which has been the better free option.

**Create the droplet** — Create → Droplets:

| Field | Value |
|---|---|
| Region | nearest to you |
| Image | Ubuntu 24.04 LTS |
| Type | Basic → Regular (SSD) |
| Size | **2GB / 1 vCPU / 50GB** (~$12/mo). 1GB/$6 is possible with 4GB swap and the lowmem override; 512MB/$4 is **not** — peak usage is ~621MB |
| Authentication | **SSH key**, not password |
| Hostname | reposage |

The 512MB droplet also fails on disk: 10GB does not hold a 3.3GB image plus
Docker's layer cache plus cloned repos.

**Point DNS (optional).** For HTTPS, add an `A` record for your domain to the
droplet's IP *before* starting the stack — Caddy requests the certificate on
first boot, and the request fails if the name doesn't resolve yet.

**Firewall.** DigitalOcean's cloud firewall (Networking → Firewalls) is
separate from `ufw`; if you create one, allow 22, 80 and 443 inbound. §3's
`ufw` rules are still worth setting as a second layer.

Then continue with §3 below — the server setup is the same everywhere. On a
2GB droplet use §4's low-memory override, and prefer §6B (pull pre-built
images) over building on the droplet.

---

## 3. One-time setup on the server

Ubuntu 24.04, as root:

```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sh

# Swap — cheap insurance against an OOM kill during a heavy pip install
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Firewall: only SSH + web
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable

git clone https://github.com/shakib1404/Reposage.git
cd Reposage                 # the repo root IS the app dir — no nested folder
cp deploy.env.example .env
nano .env          # fill in the values — see §5
```

---

## 4. If you chose a 2GB VM (B1ms)

2GB is workable for single-user use, but the defaults in `docker-compose.yml`
assume 4GB. Use the low-memory override, which lowers the container limit and
shrinks the build tmpfs so it can't outgrow RAM:

```bash
docker compose -f docker-compose.yml -f docker-compose.lowmem.yml up -d --build
```

Swap is **not optional** at this size — §3 sets up 2GB; make it 4GB on a 2GB VM:

```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
```

Expect to run one execution at a time. Building the image on a 2GB VM can also
be tight; if `docker compose build` gets OOM-killed, build it once on your
laptop, push to Docker Hub, and pull it on the VM instead.

---

## 5. Configuration

Everything lives in `.env` (never committed — `.gitignore` already blocks it).

| Variable | Notes |
|---|---|
| `SITE_ADDRESS` | `:80` for IP-only. Set it to your domain (e.g. `reposage.me`) and Caddy issues + renews a Let's Encrypt cert automatically. **Point the DNS A record at the VM first** or the cert request fails. |
| `MONGO_URL` | Your existing Atlas free tier. In Atlas → **Network Access**, whitelist the VM's public IP. |
| `JWT_SECRET` | Generate a fresh one: `openssl rand -hex 32`. Don't reuse the dev value. |
| `GROQ_API_KEY_1..4` | At least `_1`. The others are automatic fallbacks on rate-limit. |
| `GITHUB_TOKEN` | Classic token, **no scopes ticked**. Lifts search from 60 → 5000 req/hour. |
| `SERPER_API_KEY`, `JINA_API_KEY` | Repo search sources. |

> The student pack also includes a free Namecheap domain for a year — pair it
> with `SITE_ADDRESS` and you get HTTPS with no extra work.

---

## 6. Deploy

Two ways. Pick **6B** unless the server has 4GB or more.

### 6A. Build on the server

Needs real headroom: the backend image installs torch and bakes two
sentence-transformers models, so the *build* peaks well above what the
finished container uses. On 1–2GB the build itself gets OOM-killed long
before the app would.

```bash
docker compose up -d --build     # first build ~5 min (torch + models)
docker compose logs -f           # watch it come up
```

Updating later:

```bash
git pull && docker compose up -d --build
```

### 6B. Pull pre-built images from Docker Hub (recommended for small VMs)

Build on your own machine, push once, and let the server only download. The
server then needs just three files: `docker-compose.hub.yml`, `.env`, and —
if you use step 8 — `repo-task executor/.env`. No source checkout, no build.

**On your machine**, after `docker compose build`:

```bash
docker login                                   # use an access token, not your password
docker tag reposage-backend:latest YOURNAME/reposage-backend:v1
docker tag reposage-web:latest     YOURNAME/reposage-web:v1
docker push YOURNAME/reposage-backend:v1
docker push YOURNAME/reposage-web:v1
```

**On the server**, with `DOCKERHUB_USER` and `TAG` set in `.env`:

```bash
docker compose -f docker-compose.hub.yml up -d

# on a 2GB VM, layer the low-memory override on as well:
docker compose -f docker-compose.hub.yml -f docker-compose.lowmem.yml up -d
```

Updating later is a new tag plus:

```bash
docker compose -f docker-compose.hub.yml up -d
```

Pin `TAG` to a version rather than tracking `latest`, so a bad push does not
roll itself out on the next restart.

**Before publishing an image, confirm no secrets went into it.** The
`**/.env` rule in `.dockerignore` is what keeps them out, and a bare `.env`
pattern silently does not — it matches only the context root, which is how
`repo-task executor/.env` once ended up baked into a layer:

```bash
docker run --rm --entrypoint sh YOURNAME/reposage-backend:v1 -c \
  'find / -name ".env*" 2>/dev/null | grep -v ^/proc'        # expect nothing
docker history --no-trunc YOURNAME/reposage-backend:v1 | grep -iE 'API_KEY=|TOKEN=|SECRET='
```

A public Docker Hub repo is readable by anyone, image layers included. On the
free plan you get unlimited public repos but only **one** private repo — not
enough for both images.

---

Either way: wait for `Search reranker warmed up` followed by
`Application startup complete`, then open the VM's public IP (or your domain)
in a browser.

---

## 7. Security — read this before exposing it publicly

RepoSage is, by design, a service that executes untrusted code on demand. On an
open URL that is a free compute host for whoever finds it. What's already in
place and what you should add:

**Already configured in `docker-compose.yml`:**
- Backend runs as an unprivileged user (uid 10001), never root
- `mem_limit: 3g`, `pids_limit: 512`, `cpus: 1.5` — a runaway or hostile repo
  gets killed inside its container instead of taking the VM down, and
  there's always CPU headroom left to SSH in
- Repo workspaces build in a 6GB `tmpfs`, so execution churn never fills the
  real disk

**You should still do:**
1. **Close registration**, or keep the URL private. JWT auth exists, but an open
   signup form means anyone can register and run code.
2. **Keep an eye on disk**: `docker system prune -af --volumes` occasionally,
   and watch `/data/outputs` (a Docker volume) which grows with every run.
3. **Don't put secrets in the execution environment** that the executed repos
   shouldn't see.

If this is for a portfolio rather than real users, the safest option is to not
expose it continuously at all — push the image to Docker Hub, keep the repo and
a demo video public, and bring the site up only when you need to show it.

---

## 8. Notes on the image

The backend image is ~3.3GB, deliberately:

- **torch is the CPU-only build** (`2.14.0+cpu`), installed from PyTorch's CPU
  index *before* `requirements.txt`. Left to resolve on its own, pip pulls the
  CUDA build and drags in ~2.7GB of `nvidia/*` plus ~700MB of `triton` that this
  app never uses — the difference between a 3.3GB image and a 7GB one.
- **The two sentence-transformers models are baked in** (~176MB), so the first
  search doesn't pay a download and the app still starts if HuggingFace is
  unreachable.
- **`build-essential` stays in the final image on purpose.** It isn't build-time
  only: repos being analysed routinely have dependencies with C extensions that
  compile during *their* pip install, and removing it breaks those runs.

---

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| UI blank while a run is in progress | A proxy is buffering SSE. Caddy is configured with `flush_interval -1`; if you swap in nginx you need `proxy_buffering off;` |
| Execution/audit cut off partway | Proxy read timeout. The Caddyfile allows 30m; match that in any replacement. |
| `pymongo ServerSelectionTimeout` | Droplet IP isn't whitelisted in Atlas Network Access. |
| Cert request fails on startup | DNS A record isn't pointing at the VM yet, or 80/443 aren't open in `ufw`. |
| Container OOM-killed during a run | Raise `mem_limit`, or move to a larger VM. |
