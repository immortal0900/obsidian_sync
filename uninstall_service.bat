@echo off
REM ============================================================
REM  obsidian_sync Windows 서비스 제거
REM  관리자 권한으로 실행하세요
REM ============================================================
setlocal EnableDelayedExpansion

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 관리자 권한이 필요합니다.
    pause
    exit /b 1
)

set NSSM=C:\ProgramData\chocolatey\bin\nssm.exe

echo === obsidian_sync 서비스 제거 ===
echo.

for %%S in (ObsidianSync ObsidianSyncBlog) do (
    sc query %%S >nul 2>&1
    if !errorlevel! == 0 (
        echo --- %%S ---
        "%NSSM%" stop %%S >nul 2>&1
        "%NSSM%" remove %%S confirm
    ) else (
        echo [INFO] %%S 서비스 없음 ^(건너뜀^)
    )
)

echo.
echo === 제거 완료 ===
sc query ObsidianSync 2>nul | findstr STATE
sc query ObsidianSyncBlog 2>nul | findstr STATE
echo ^(위에 표시 없으면 모두 제거됨^)
pause
