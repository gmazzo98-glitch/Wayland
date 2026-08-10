@echo off
TITLE Project Vienna - GG Signal Sourcing and Scoring Dashboard
COLOR 0A

:: Force current directory to the folder containing this batch file
cd /d "%~dp0"

echo =========================================================================
echo  Project Vienna - GG Signal Sourcing and Scoring Dashboard
echo =========================================================================
echo Working Directory: %CD%
echo.

:: 1. Verify Python availability
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found on your system PATH!
    echo Please install Python 3.10 or higher and check "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo [OK] Python detected. Checking dependencies...

:: 2. Check and auto-install required packages if missing
python -c "import streamlit, sqlalchemy, plotly, pandas" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing required dependencies from requirements.txt...
    python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies. Check your internet connection.
        pause
        exit /b 1
    )
)

echo [OK] All dependencies verified.

:: 3. Seed initial database if vienna.db does not exist
if not exist "vienna.db" (
    echo [INFO] Database not found. Seeding initial Agrifood company dataset...
    python seed.py
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to seed database.
        pause
        exit /b 1
    )
    echo [OK] Database seeded successfully.
)

echo.
echo =========================================================================
echo  Starting Streamlit Application...
echo  Your default browser will open automatically at: http://localhost:8501
echo  To stop the dashboard, close this window or press Ctrl+C.
echo =========================================================================
echo.

:: 4. Launch Streamlit dashboard
python -m streamlit run app.py --server.headless false

:: Keep window open if streamlit exits unexpectedly
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Streamlit exited with error code %errorlevel%.
    pause
)
