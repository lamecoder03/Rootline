@echo off
REM Loads .env into real OS environment variables, then runs dbt from the Python 3.13 venv.
REM Exists because dbt's env_var() reads the process environment, and nothing populates it
REM from .env — python-dotenv is a Python-library call the dbt process never makes.
REM Anchors every path to the repo root, so it works from any working directory.

setlocal

set "REPO_ROOT=%~dp0"
set "ENV_FILE=%REPO_ROOT%.env"
set "DBT_EXE=%REPO_ROOT%.venv-dbt\Scripts\dbt.exe"
set "DBT_PROJECT=%REPO_ROOT%dbt\revenue_anomaly"

if not exist "%ENV_FILE%" (
    echo [run_dbt] .env not found at "%ENV_FILE%"
    echo [run_dbt] Copy .env.example to .env and set POSTGRES_PASSWORD first.
    exit /b 1
)

if not exist "%DBT_EXE%" (
    echo [run_dbt] dbt not found at "%DBT_EXE%"
    echo [run_dbt] Create the venv first ^(Python 3.13 — dbt cannot run on 3.14^):
    echo [run_dbt]     py -3.13 -m venv .venv-dbt
    echo [run_dbt]     .venv-dbt\Scripts\pip install -r requirements-dbt.txt
    exit /b 1
)

if "%~1"=="" (
    echo [run_dbt] Usage: run_dbt.bat ^<dbt command^> [args]
    echo [run_dbt] e.g.   run_dbt.bat debug
    echo [run_dbt]        run_dbt.bat build
    echo [run_dbt]        run_dbt.bat test --select stg_product_master
    exit /b 1
)

REM findstr keeps only real KEY=VALUE lines, so comments, blank lines and the UTF-8 BOM
REM never reach `set`. tokens=1,* splits on the first = only, so values may contain =.
for /f "usebackq tokens=1,* delims==" %%K in (
    `findstr /r /c:"^[A-Za-z_][A-Za-z0-9_]*=" "%ENV_FILE%"`
) do set "%%K=%%L"

if not defined POSTGRES_PASSWORD (
    echo [run_dbt] POSTGRES_PASSWORD is not set in .env — dbt will fail to authenticate.
    exit /b 1
)

"%DBT_EXE%" %* --project-dir "%DBT_PROJECT%" --profiles-dir "%DBT_PROJECT%"
exit /b %ERRORLEVEL%
