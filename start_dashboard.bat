@echo off
chcp 65001 >nul 2>&1

echo ============================================
echo   Quantfolio - Dashboard
echo ============================================
echo.

REM Change to script directory
cd /d "%~dp0"
echo Working directory: %CD%
echo.

REM Try activating conda
echo Activating Miniconda...
call "%USERPROFILE%\miniconda3\condabin\activate.bat" 2>nul
if errorlevel 1 (
    echo Could not activate conda via condabin.
    echo Trying Scripts...
    call "%USERPROFILE%\miniconda3\Scripts\activate.bat" 2>nul
)

echo.
echo Checking Python...
python --version
if errorlevel 1 (
    echo.
    echo ERROR: Python not found.
    echo Please open Anaconda Prompt manually and run:
    echo   cd %CD%
    echo   python api_server.py
    echo.
    pause
    exit /b 1
)

echo.
echo Checking dependencies...
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install -r requirements.txt
    echo.
)

echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.

start "" cmd /c "timeout /t 3 >nul & start http://localhost:8000"

python api_server.py

echo.
echo Server stopped. See error above.
echo.
pause
