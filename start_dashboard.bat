@echo off
chcp 65001 >nul 2>&1
setlocal

echo ============================================
echo   Quantfolio - Dashboard
echo ============================================
echo.

REM Change to script directory
cd /d "%~dp0"
echo Working directory: %CD%
echo.

REM --- C-10: parse flags ---------------------------------------------------
REM   start_dashboard.bat              (default) hard deps auto-install;
REM                                    lightgbm optional with warning
REM   start_dashboard.bat --full-install  also install lightgbm
set FULL_INSTALL=0
if /I "%~1"=="--full-install" set FULL_INSTALL=1
if /I "%~1"=="/full-install"  set FULL_INSTALL=1

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

REM --- C-10: probe the FULL hard-dep set (import-check each) ---------------
REM If any are missing we run a single `pip install -r requirements.txt` and
REM re-probe once. This avoids the old behaviour where only `fastapi` was
REM checked, letting an ENOENT on e.g. apscheduler kill the server only
REM after it had already opened the browser.
echo.
echo Checking dependencies...
python -c "import fastapi, uvicorn, pandas, numpy, sklearn, yfinance, xgboost, apscheduler" >nul 2>&1
if errorlevel 1 (
    echo One or more required dependencies are missing.
    echo Installing from requirements.txt...
    python -m pip install -r requirements.txt
    echo.
    python -c "import fastapi, uvicorn, pandas, numpy, sklearn, yfinance, xgboost, apscheduler" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo ERROR: Dependencies still missing after install. See messages above.
        echo.
        pause
        exit /b 1
    )
)

REM --- C-10: lightgbm is OPTIONAL. Warn instead of hard-failing. -----------
python -c "import lightgbm" >nul 2>&1
if errorlevel 1 (
    if "%FULL_INSTALL%"=="1" (
        echo Installing lightgbm ^(enables Pro / v3 predictions^)...
        python -m pip install lightgbm
        python -c "import lightgbm" >nul 2>&1
        if errorlevel 1 (
            echo.
            echo WARNING: lightgbm failed to install. Pro predictions will show "Not available".
            echo          Try installing Microsoft Visual C++ Build Tools or ask your developer.
            echo.
        )
    ) else (
        echo.
        echo WARNING: lightgbm is not installed. Pro ^(v3^) predictions will show "Not available".
        echo          To enable Pro, close this window and run: start_dashboard.bat --full-install
        echo          ^(or ask your developer^).
        echo.
    )
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
endlocal
