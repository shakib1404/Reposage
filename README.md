# RepoSage 🔬

Autonomous repository exploration & task execution agent.

Describe a task in plain English. RepoSage searches GitHub for a project that
solves it, ranks the candidates by whether they will actually *run*, maps the
codebase, then executes it in a sandbox — fixing its own failures until it works.

## What it does

| Step | |
|---|---|
| 1. **Find repos** | Plain-English task; an LLM normalises it into a search query |
| 2. **Select repo** | 9 Python candidates scored on semantic fit, popularity and **runnability** (dependency manifests, entrypoints, Docker support, commit recency) |
| 3. **Analyze** | Clone + AST parse → Hierarchical Code Tree, Function Call Graph, Module Dependency Graph, and a file listing that matches github.com exactly |
| 4. **Architecture** | Three Mermaid diagrams, switchable: **Architecture** (subsystems and boundaries), **Sequence** (one end-to-end runtime flow as a timeline), **Data flow** (where data enters, is transformed, rests and leaves) |
| 5. **Execute** | Installs deps and runs the entrypoint; on failure the traceback goes back to the model, which rewrites files and retries |
| 6. **Output** | Streamed logs, exit code, downloadable artifacts |
| 7. **Audit** | Lint, security, CVEs, types, secrets, dead code, patterns → letter grade + 2 PDF reports |
| 8. **RepoTask Exec** | Point any repo at a new task; the agent fixes it, runs the tests, and **opens a pull request** |

Plus a **Copy Detector** (structural similarity between repos), **codebase chat**
(RAG over the clone), and **History** (reopen any past run with its graphs intact).

> ⚠️ **RepoSage runs third-party code on purpose.** Runs are confined to a
> throw-away workspace with capped memory, CPU and process count, but this is
> not a hardened multi-tenant sandbox. Run it on infrastructure you are willing
> to lose, and keep registration closed to people you trust.

---

## Quick start — Docker Hub (no build, no checkout)

The published images already contain everything, including the two
sentence-transformers models. This is the fastest path and the only practical
one on a small VM: *building* the backend needs far more RAM than running it.

```bash
# 1. Get the two compose files and the env template
curl -O https://raw.githubusercontent.com/shakib1404/Reposage/master/docker-compose.hub.yml
curl -O https://raw.githubusercontent.com/shakib1404/Reposage/master/docker-compose.lowmem.yml
curl -o .env https://raw.githubusercontent.com/shakib1404/Reposage/master/deploy.env.example

# 2. Fill in .env — at minimum MONGO_URL, JWT_SECRET, GROQ_API_KEY_1
nano .env
echo "DOCKERHUB_USER=shakib1404" >> .env
echo "TAG=v2"                    >> .env

# 3. Run
docker compose -f docker-compose.hub.yml up -d

# …or on a 2GB host, layer the low-memory override on top:
docker compose -f docker-compose.hub.yml -f docker-compose.lowmem.yml up -d

docker compose -f docker-compose.hub.yml logs -f
```

Wait for `Search reranker warmed up` then `Application startup complete`, and
open **http://localhost** (or the server's IP).

**Published images**

| Image | Tags | Size (compressed) |
|---|---|---|
| [`shakib1404/reposage-backend`](https://hub.docker.com/r/shakib1404/reposage-backend) | `:v2` `:latest`, `:v1` | ~830 MB |
| [`shakib1404/reposage-web`](https://hub.docker.com/r/shakib1404/reposage-web) | `:v2` `:latest`, `:v1` | ~25 MB |

Pin `TAG` to a version rather than tracking `latest`, so a bad push does not
roll itself out on your next restart. `v1` is kept as the rollback point —
`TAG=v1` and a restart puts the previous build back.

`v2` adds the sequence / data-flow diagrams, the multi-model Groq fallback, and
drops the audit's autofix endpoint.

### Build it yourself instead

```bash
git clone https://github.com/shakib1404/Reposage.git
cd Reposage
cp deploy.env.example .env && nano .env
docker compose up -d --build        # ~5 min; needs ≥4GB RAM for the build
```

Full server setup — VM sizing, swap, firewall, TLS, publishing your own
images — is in **[DEPLOY.md](DEPLOY.md)**.

---

## Configuration

Everything lives in `.env` next to the compose file. Minimum to boot:

| Key | Notes |
|---|---|
| `MONGO_URL` | MongoDB Atlas; the free M0 tier is enough |
| `JWT_SECRET` | `openssl rand -hex 32` |
| `GROQ_API_KEY_1` | https://console.groq.com (free). `_2.._4` are rate-limit fallbacks |
| `SITE_ADDRESS` | `:80`, or a domain — Caddy then issues a Let's Encrypt cert itself |

Optional:

| Key | Enables |
|---|---|
| `GROQ_MODEL`, `GROQ_MODELS` | Groq meters tokens **per model**, so when all four keys are out of daily quota the chain switches model and gets a fresh allowance. `GROQ_MODEL` sets the primary; `GROQ_MODELS` (comma-separated) replaces the whole chain. Check ids against `/v1/models` first — most of Groq's catalogue 404s on the free tier |
| `GITHUB_TOKEN` | Lifts the GitHub API limit from 60 to 5000 req/h. **Search needs no scopes; step 8's push + PR needs the `repo` scope.** |
| `SERPER_API_KEY`, `JINA_API_KEY` | Extra repo-search sources |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | Step 8 (RepoTask Exec) — it drives Claude directly, not Groq |
| `GMAIL_USER`, `GMAIL_PASS` | Password-reset email |

`.env` files are never copied into the image — they are read at run time.

---

## Local development

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:5173.

To develop the UI against a backend that is already running in Docker (its port
is internal, so requests go in through Caddy):

```bash
VITE_API_TARGET=http://127.0.0.1:80 npm run dev
```

## Stack

- **Frontend** — React + Vite, Recharts, Mermaid, jsPDF
- **Backend** — FastAPI (Python 3.13), MongoDB Atlas
- **LLMs** — Groq for ranking, analysis and the execution loop, over 4 API keys ×
  a chain of models (`gpt-oss-120b` → `gpt-oss-20b` → `qwen3.8-27b` →
  `gpt-oss-safeguard-20b`); Anthropic Claude for the RepoTask Executor
- **Retrieval** — sentence-transformers bi-encoder + cross-encoder reranking,
  CPU-only torch
- **Serving** — Caddy (static SPA + `/api` reverse proxy with SSE flushing)

## Layout

```
Reposage/                       # repo root
├── frontend/                   # React + Vite UI
│   └── src/
│       ├── App.jsx             # step router, auth gate, history persistence
│       ├── pages/              # LandingPage, Search, Select, Analyze,
│       │                       # Architect, Execute, Output, Test, TaskExec,
│       │                       # CopyDetect, History
│       └── components/         # AuthCard, TreeView, FileTree, GraphCanvas,
│                               # ClusterView, LoopFeed, ScoreBar, MiniMarkdown
├── backend/                    # FastAPI
│   ├── main.py                 # routes + SSE streams
│   ├── auth.py / history_db.py # accounts, saved runs
│   ├── search.py               # retrieve → rerank → runnability probe
│   ├── analyzer.py             # HCT / FCG / MDG, metrics, file tree
│   ├── architect.py            # Mermaid architecture / sequence / dataflow diagrams
│   ├── executor.py             # self-healing execute loop
│   ├── tester.py              # audit scanners (9 engines)
│   ├── chat.py / rag.py        # codebase chat
│   ├── dupdetect.py / corpus.py / copydetector/
│   └── llm.py                  # Groq client, key rotation, token-budget guard
├── repo-task executor/         # step 8 — separate Claude-driven agent
│   ├── run_task.py             # entry point the backend subprocesses
│   └── agent/                  # cloner, reader, writer, tester, github_pr
├── docker/                     # Dockerfile.backend, Dockerfile.web, Caddyfile
├── docker-compose.yml          # build locally
├── docker-compose.hub.yml      # pull published images
├── docker-compose.lowmem.yml   # 2GB-host override
├── deploy.env.example
└── DEPLOY.md
```
