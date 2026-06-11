# RepoSage 🔬

Autonomous repository exploration & task execution agent.

## What it does
1. **Find repos** – Enter a natural-language task; it searches GitHub for the top 3 relevant repos
2. **Select repo** – Pick the best match
3. **Analyze** – Builds HCT (Hierarchical Code Tree), FCG (Function Call Graph), MDG (Module Dependency Graph), scores every module
4. **Execute** – Autonomous explore→write→run→debug loop using Groq LLM
5. **Output** – View results and full analysis dashboard

## Stack
- **Frontend**: React + Vite + Recharts + D3
- **Backend**: FastAPI (Python)
- **LLMs**: Groq (`GROQ_MODEL` for chat tasks, `GROQ_CLASSIFIER_MODEL` for single-message classification) for repo ranking, analysis, and execution loop. Default chat model: `llama-3.3-70b-versatile`.
- **Search**: Serper API (GitHub search), Jina AI (content fetch)

## Setup

### 1. Get API keys
- Groq: https://console.groq.com (free)
- Serper: https://serper.dev (free tier: 2500 queries)
- Jina: https://jina.ai (free tier)

### 2. Configure
```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Install & run
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

Open http://localhost:5173

## Architecture

```
reposage/
├── frontend/          # React + Vite UI
│   └── src/
│       ├── App.jsx
│       ├── pages/
│       │   ├── SearchPage.jsx
│       │   ├── SelectPage.jsx
│       │   ├── AnalyzePage.jsx
│       │   ├── ExecutePage.jsx
│       │   └── OutputPage.jsx
│       ├── components/
│       │   ├── TreeView.jsx
│       │   ├── GraphCanvas.jsx
│       │   ├── ClusterView.jsx
│       │   ├── LoopFeed.jsx
│       │   └── ScoreBar.jsx
│       └── api.js
├── backend/           # FastAPI
│   ├── main.py
│   ├── search.py      # Serper + Jina search
│   ├── analyzer.py    # Repo analysis & scoring
│   ├── executor.py    # LLM execution loop
│   └── requirements.txt
├── .env.example
└── README.md
```
