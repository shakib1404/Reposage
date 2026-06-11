#!/bin/bash
set -e

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║         RepoSage — Starting          ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# Check .env
if [ ! -f .env ]; then
  echo "⚠  No .env found. Copying .env.example → .env"
  cp .env.example .env
  echo "   → Edit .env with your API keys before running!"
  echo ""
fi

# Backend
echo "▶ Starting backend (FastAPI on :8000)…"
cd backend
pip install -r requirements.txt -q
uvicorn main:app --reload --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
cd ..

sleep 2

# Frontend
echo "▶ Starting frontend (Vite on :5173)…"
cd frontend
npm install --silent
npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo "✓ RepoSage running!"
echo "  Frontend: http://localhost:5173"
echo "  Backend:  http://localhost:8000"
echo "  API docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Stopped.'" INT TERM
wait $BACKEND_PID $FRONTEND_PID
