@echo off
title Asquareic Engineering Unified Dashboard
echo Installing / verifying dependencies...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo WARNING: pip install failed. Attempting to start server anyway...
)
echo Starting local web server...
start "" "http://127.0.0.1:5000"
python app.py
pause
