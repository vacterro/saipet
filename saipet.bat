@echo off
setlocal
rem ============================================================
rem  SAIPET launcher (Windows)
rem
rem  Runs the project from its own directory so state files
rem  (seen.json, review-state.json, runs/, monitor-status.json)
rem  land in one consistent place, and keeps a virtualenv warm.
rem
rem  Usage:
rem    saipet              -> desktop GUI
rem    saipet cli          -> one-shot scout (add flags after)
rem    saipet monitor      -> unattended monitor daemon (add flags)
rem    saipet bridge       -> line-oriented command engine (stdin)
rem ============================================================

cd /d "%~dp0"

set "MODE=%~1"
if not defined MODE set "MODE=gui"

rem CORE-009: capture every argument AFTER the mode verb and forward the
rem complete set to the selected module. `%*` is not usable here -- it always
rem expands to ALL original arguments regardless of `shift` -- so the loop
rem below rebuilds the tail from `%1` while preserving each argument's
rem original quoting. Unlike the old fixed `%2 %3 ... %9`, this never
rem truncates a valid command line (e.g. a repeatable --subreddit or a
rem trailing --quiet beyond position 9).
shift
set "ARGS="
:parse_args
if "%~1"=="" goto :args_done
if defined ARGS (
    set "ARGS=%ARGS% %1"
) else (
    set "ARGS=%1"
)
shift
goto :parse_args
:args_done

if not exist ".venv" (
    echo [saipet] creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [saipet] failed to create .venv -- is python on PATH?
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

if not exist ".venv\.saipen-deps" (
    echo [saipet] installing dependencies...
    python -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [saipet] dependency install failed.
        exit /b 1
    )
    type nul > ".venv\.saipen-deps"
)

if "%MODE%"=="gui" (
    python -m saipet.gui %ARGS%
) else if "%MODE%"=="cli" (
    python -m saipet.cli %ARGS%
) else if "%MODE%"=="monitor" (
    python -m saipet.monitor %ARGS%
) else if "%MODE%"=="bridge" (
    python -m saipet.bridge %ARGS%
) else (
    echo [saipet] unknown mode '%MODE%' -- use: gui ^| cli ^| monitor ^| bridge
    exit /b 1
)

endlocal
