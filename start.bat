@echo off
echo.
echo ╔═══════════════════════════════════════╗
echo ║         RepoSage — Starting          ║
echo ╚═══════════════════════════════════════╝
echo.

if not exist .env (
  echo [WARN] No .env found. Copying .env.example to .env
  copy .env.example .env
  echo Edit .env with your API keys then re-run this script.
  pause
  exit /b 1
)

echo [1/2] Starting backend on http://localhost:8000 ...
cd backend
pip install -r requirements.txt -q
start "RepoSage Backend" cmd /k "uvicorn main:app --reload --port 8000"
cd ..

timeout /t 3 /nobreak > nul

echo [2/2] Starting frontend on http://localhost:5173 ...
cd frontend
call npm install --silent
start "RepoSage Frontend" cmd /k "npm run dev"
cd ..

echo.
echo ✓ Both servers starting in separate windows.
echo   Frontend: http://localhost:5173
echo   Backend:  http://localhost:8000/docs
echo.
pause
