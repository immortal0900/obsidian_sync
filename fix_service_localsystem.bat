@echo off
setlocal EnableDelayedExpansion

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator privileges required.
    echo Right-click this file and choose "Run as administrator".
    pause
    exit /b 1
)

set NSSM=C:\ProgramData\chocolatey\bin\nssm.exe

echo ========================================
echo Switching services to LocalSystem
echo ========================================

for %%S in (ObsidianSync ObsidianSyncBlog) do (
    echo.
    echo [%%S] stopping if running
    "%NSSM%" stop %%S >nul 2>&1

    echo [%%S] setting account = LocalSystem
    "%NSSM%" set %%S ObjectName LocalSystem
    if errorlevel 1 (
        echo [%%S] account change failed
    )

    echo [%%S] starting
    "%NSSM%" start %%S
    if errorlevel 1 (
        echo [%%S] start failed - see service_stderr log
    ) else (
        echo [%%S] OK
    )
)

echo.
echo ========================================
echo Status
echo ========================================
sc query ObsidianSync | findstr STATE
sc query ObsidianSyncBlog | findstr STATE

echo.
pause
