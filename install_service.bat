@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM  obsidian_sync Windows service installer
REM  RIGHT-CLICK this file and choose "Run as administrator"
REM ============================================================

echo.
echo ========================================
echo   obsidian_sync service installer
echo ========================================
echo.

REM --- Admin check ---
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator privileges required.
    echo         Right-click this .bat file and choose
    echo         "Run as administrator".
    pause
    exit /b 1
)

set NSSM=C:\ProgramData\chocolatey\bin\nssm.exe
set UV=C:\Users\immor\.local\bin\uv.exe
set DIR=C:\1.Project\obsidian_sync

REM --- Prerequisite check ---
if not exist "%NSSM%" (
    echo [ERROR] nssm.exe not found at:
    echo         %NSSM%
    pause
    exit /b 1
)
if not exist "%UV%" (
    echo [ERROR] uv.exe not found at:
    echo         %UV%
    pause
    exit /b 1
)
if not exist "%DIR%\run_forever.py" (
    echo [ERROR] run_forever.py not found in:
    echo         %DIR%
    pause
    exit /b 1
)

echo [OK] nssm found
echo [OK] uv found
echo [OK] run_forever.py found
echo.

REM --- Account setup ---
echo Services must run as your Windows user account
echo so they can access token.json in your home folder.
echo.
set /p SETACCOUNT="Use your account (%USERNAME%)? [Y/N]: "
set PASSWORD=
if /i "!SETACCOUNT!"=="Y" (
    echo.
    echo Enter Windows login password for %USERNAME%
    echo [visible on screen]:
    set /p PASSWORD="Password: "
)

echo.
echo ========================================
echo   Installing service 1 of 2
echo ========================================

call :Install ObsidianSync config.yaml service_stdout.log service_stderr.log
if errorlevel 1 (
    echo [FAIL] Service 1 install failed
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Installing service 2 of 2
echo ========================================

call :Install ObsidianSyncBlog config_blog.yaml service_stdout_blog.log service_stderr_blog.log
if errorlevel 1 (
    echo [FAIL] Service 2 install failed
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Final status
echo ========================================
sc query ObsidianSync | findstr STATE
sc query ObsidianSyncBlog | findstr STATE

echo.
echo Install complete.
echo   App log     : %DIR%\obsidian_sync.log
echo   Blog log    : %DIR%\obsidian_sync_blog.log
echo   Service log : %DIR%\service_stdout.log
echo   Manage      : services.msc
echo   Uninstall   : uninstall_service.bat as admin
echo.
pause
exit /b 0


:Install
set SVC=%~1
set CFG=%~2
set LOGOUT=%DIR%\%~3
set LOGERR=%DIR%\%~4

echo.
echo [%SVC%] target config: %CFG%

REM Remove existing
sc query %SVC% >nul 2>&1
if not errorlevel 1 (
    echo [%SVC%] removing existing service
    "%NSSM%" stop %SVC% >nul 2>&1
    "%NSSM%" remove %SVC% confirm >nul 2>&1
    timeout /t 2 /nobreak >nul
)

echo [%SVC%] installing
"%NSSM%" install %SVC% "%UV%" run python run_forever.py %CFG%
if errorlevel 1 (
    echo [%SVC%] install command failed
    exit /b 1
)

"%NSSM%" set %SVC% AppDirectory "%DIR%" >nul
"%NSSM%" set %SVC% AppStdout "%LOGOUT%" >nul
"%NSSM%" set %SVC% AppStderr "%LOGERR%" >nul
"%NSSM%" set %SVC% AppRotateFiles 1 >nul
"%NSSM%" set %SVC% AppRotateBytes 10485760 >nul
"%NSSM%" set %SVC% AppExit Default Restart >nul
"%NSSM%" set %SVC% AppRestartDelay 5000 >nul
"%NSSM%" set %SVC% Start SERVICE_AUTO_START >nul
"%NSSM%" set %SVC% Description "obsidian_sync bidirectional sync" >nul

if /i "!SETACCOUNT!"=="Y" (
    "%NSSM%" set %SVC% ObjectName ".\%USERNAME%" "!PASSWORD!" >nul
    if errorlevel 1 (
        echo [%SVC%] account set failed, falling back to LocalSystem
    )
)

echo [%SVC%] starting
"%NSSM%" start %SVC%
if errorlevel 1 (
    echo [%SVC%] start failed, check service_stderr log
) else (
    echo [%SVC%] OK
)
exit /b 0
