@echo off
setlocal enabledelayedexpansion
title Asquareic Engineering Unified Dashboard Launcher

echo ==========================================================
echo   Asquareic Engineering Unified Dashboard Launcher ^& Setup
echo ==========================================================
echo.

set PYTHON_CMD=

:: 1. Search PATH for python/py
python -c "import sys" >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python
) else (
    py -c "import sys" >nul 2>&1
    if %errorlevel% equ 0 (
        set PYTHON_CMD=py
    )
)

:: 2. If not found in PATH, check standard installation directories
if "%PYTHON_CMD%" == "" (
    echo Searching standard paths for Python...
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
        if exist "%%D\python.exe" (
            set "PATH=%%D;%%D\Scripts;!PATH!"
        )
    )
    for /d %%D in ("%ProgramFiles%\Python*") do (
        if exist "%%D\python.exe" (
            set "PATH=%%D;%%D\Scripts;!PATH!"
        )
    )
    
    :: Re-check after path update
    python -c "import sys" >nul 2>&1
    if %errorlevel% equ 0 (
        set PYTHON_CMD=python
    ) else (
        py -c "import sys" >nul 2>&1
        if %errorlevel% equ 0 (
            set PYTHON_CMD=py
        )
    )
)

:: 3. If still not found, install Python
if "%PYTHON_CMD%" == "" (
    echo Python not found. Installing Python 3.11...
    
    :: Try winget
    winget install --id Python.Python.3.11 --exact --silent --accept-source-agreements --accept-package-agreements >nul 2>&1
    if %errorlevel% equ 0 (
        echo Python 3.11 installed successfully via winget.
    ) else (
        echo Winget not available or failed. Downloading Python 3.11 installer...
        curl -L -o "%temp%\python-installer.exe" https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
        echo Installing Python silently...
        start /wait "" "%temp%\python-installer.exe" /quiet PrependPath=1 Include_test=0
        del "%temp%\python-installer.exe"
        echo Python installation completed.
    )

    :: Add the newly installed python to PATH of current session
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
        if exist "%%D\python.exe" (
            set "PATH=%%D;%%D\Scripts;!PATH!"
        )
    )
    for /d %%D in ("%ProgramFiles%\Python*") do (
        if exist "%%D\python.exe" (
            set "PATH=%%D;%%D\Scripts;!PATH!"
        )
    )

    python -c "import sys" >nul 2>&1
    if %errorlevel% equ 0 (
        set PYTHON_CMD=python
    ) else (
        py -c "import sys" >nul 2>&1
        if %errorlevel% equ 0 (
            set PYTHON_CMD=py
        )
    )
)

:: Verify we finally have Python and it is Python 3
if "%PYTHON_CMD%" == "" (
    echo.
    echo ERROR: Python could not be installed or added to PATH automatically.
    echo Please install Python 3 manually from https://www.python.org/
    pause
    exit /b 1
)

set PYTHON_MAJOR=
for /f %%i in ('!PYTHON_CMD! -c "import sys; print(sys.version_info.major)" 2^>nul') do (
    set PYTHON_MAJOR=%%i
)

if "%PYTHON_MAJOR%" neq "3" (
    echo ERROR: Python 3 is required. Found Python major version: %PYTHON_MAJOR%
    pause
    exit /b 1
)

echo Using Python: !PYTHON_CMD! (Version 3)

:: 4. Verify/Install ODA File Converter
echo.
echo Checking for ODA File Converter...
set ODA_FOUND=0
where ODAFileConverter.exe >nul 2>&1
if !errorlevel! equ 0 (
    set ODA_FOUND=1
    echo ODA File Converter is already in PATH.
) else (
    for /d %%D in ("%ProgramFiles%\ODA\ODAFileConverter*") do (
        if exist "%%D\ODAFileConverter.exe" (
            set "PATH=%%D;!PATH!"
            set ODA_FOUND=1
            echo Found ODA File Converter in program files and added to session PATH.
            powershell -Command "$p = [Environment]::GetEnvironmentVariable('Path', 'User'); if ($p -notlike '*%%D*') { [Environment]::SetEnvironmentVariable('Path', $p + ';%%D', 'User'); echo 'Permanent PATH updated.' }" >nul 2>&1
        )
    )
)

if !ODA_FOUND! equ 0 (
    echo ODA File Converter not found. Installing via winget...
    winget install -e --id ODA.ODAFileConverter --silent --accept-source-agreements --accept-package-agreements >nul 2>&1
    if !errorlevel! equ 0 (
        echo ODA File Converter installed successfully.
        :: Re-check and update PATH
        for /d %%D in ("%ProgramFiles%\ODA\ODAFileConverter*") do (
            if exist "%%D\ODAFileConverter.exe" (
                set "PATH=%%D;!PATH!"
                set ODA_FOUND=1
                powershell -Command "$p = [Environment]::GetEnvironmentVariable('Path', 'User'); if ($p -notlike '*%%D*') { [Environment]::SetEnvironmentVariable('Path', $p + ';%%D', 'User') }" >nul 2>&1
            )
        )
    ) else (
        echo WARNING: Failed to install ODA File Converter via winget automatically.
        echo Please install it manually from: https://www.opendesign.com/guestfiles/oda_file_converter
    )
)


:: 5. Verify/Install dependencies
echo.
echo Checking and installing Python dependencies...
!PYTHON_CMD! -m pip install --upgrade pip
!PYTHON_CMD! -m pip install -r "%~dp0requirements.txt"

if %errorlevel% neq 0 (
    echo.
    echo WARNING: Failed to install some dependencies. Attempting to run anyway...
    echo.
)

echo.
echo Starting local web server...
start "" "http://127.0.0.1:5000"

!PYTHON_CMD! "%~dp0app.py" %*

if %errorlevel% neq 0 (
    echo.
    echo Dashboard exited with error code %errorlevel%.
    pause
)

