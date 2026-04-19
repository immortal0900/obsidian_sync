@echo off
REM ============================================================
REM  obsidian_sync Windows 서비스 설치 (2개)
REM    1. ObsidianSync      — config.yaml      (obsidian_world)
REM    2. ObsidianSyncBlog  — config_blog.yaml (quartz/content)
REM  관리자 권한으로 실행하세요 (우클릭 → 관리자 권한으로 실행)
REM ============================================================
setlocal EnableDelayedExpansion

REM 관리자 권한 확인
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 관리자 권한이 필요합니다.
    echo         이 파일을 우클릭 → "관리자 권한으로 실행" 하세요.
    pause
    exit /b 1
)

set NSSM=C:\ProgramData\chocolatey\bin\nssm.exe
set UV=C:\Users\immor\.local\bin\uv.exe
set DIR=C:\1.Project\obsidian_sync

echo.
echo === obsidian_sync 서비스 2개 설치 ===
echo  작업 디렉토리 : %DIR%
echo  실행 명령     : %UV% run python run_forever.py ^<config^>
echo.

REM 사용자 계정 설정 여부 묻기 (두 서비스 공통)
echo [주의] 서비스가 OAuth token.json 에 접근해야 하므로
echo        본인 Windows 계정으로 실행해야 합니다. (LocalSystem 불가)
echo.
set /p SETACCOUNT="본인 계정(%USERNAME%)으로 설정할까요? (Y/N): "
set PASSWORD=
if /i "%SETACCOUNT%"=="Y" (
    echo.
    echo Windows 로그인 패스워드 입력 (화면에 표시됨):
    set /p PASSWORD="Password: "
)

REM ============================================================
REM  서비스 1: ObsidianSync (메인 볼트)
REM ============================================================
call :InstallService ObsidianSync config.yaml service_stdout.log service_stderr.log "Obsidian vault -> Google Drive sync (main)"

REM ============================================================
REM  서비스 2: ObsidianSyncBlog (블로그 content)
REM ============================================================
call :InstallService ObsidianSyncBlog config_blog.yaml service_stdout_blog.log service_stderr_blog.log "Quartz blog content -> Google Drive sync"

echo.
echo ============================================================
echo === 최종 상태 ===
sc query ObsidianSync | findstr STATE
sc query ObsidianSyncBlog | findstr STATE
echo.
echo 로그 확인:
echo   메인 앱 로그   : %DIR%\obsidian_sync.log
echo   블로그 앱 로그 : %DIR%\obsidian_sync_blog.log
echo   서비스 stdout  : %DIR%\service_stdout.log / service_stdout_blog.log
echo.
echo 서비스 관리 : services.msc (GUI)
echo 제거       : uninstall_service.bat 관리자 권한 실행
echo ============================================================
pause
exit /b 0


REM ============================================================
REM  서브루틴: 단일 서비스 설치
REM  %1 = 서비스명, %2 = config 파일, %3 = stdout 로그, %4 = stderr 로그, %5 = 설명
REM ============================================================
:InstallService
set SVC=%~1
set CFG=%~2
set LOGOUT=%DIR%\%~3
set LOGERR=%DIR%\%~4
set DESC=%~5

echo.
echo --- [%SVC%] 설치 중 (config=%CFG%) ---

REM 기존 서비스 제거
sc query %SVC% >nul 2>&1
if %errorlevel% == 0 (
    "%NSSM%" stop %SVC% >nul 2>&1
    "%NSSM%" remove %SVC% confirm >nul 2>&1
)

"%NSSM%" install %SVC% "%UV%" run python run_forever.py %CFG%
if errorlevel 1 (
    echo   [ERROR] %SVC% 설치 실패
    exit /b 1
)

"%NSSM%" set %SVC% AppDirectory "%DIR%"
"%NSSM%" set %SVC% AppStdout "%LOGOUT%"
"%NSSM%" set %SVC% AppStderr "%LOGERR%"
"%NSSM%" set %SVC% AppRotateFiles 1
"%NSSM%" set %SVC% AppRotateBytes 10485760
"%NSSM%" set %SVC% AppExit Default Restart
"%NSSM%" set %SVC% AppRestartDelay 5000
"%NSSM%" set %SVC% Start SERVICE_AUTO_START
"%NSSM%" set %SVC% Description "%DESC%"

if /i "%SETACCOUNT%"=="Y" (
    "%NSSM%" set %SVC% ObjectName ".\%USERNAME%" "!PASSWORD!"
    if errorlevel 1 (
        echo   [WARN] %SVC% 계정 설정 실패 - LocalSystem 사용
    )
)

"%NSSM%" start %SVC%
if errorlevel 1 (
    echo   [ERROR] %SVC% 시작 실패
) else (
    echo   [OK] %SVC% 설치 및 시작 완료
)

exit /b 0
