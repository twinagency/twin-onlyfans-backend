@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem The legacy script-suite runner. Both Python's exit status and the house
rem success marker are required. The mixed pytest runner remains
rem scripts/run-service-tests.py.
pushd "%~dp0" || exit /b 2
set "PY=python"
if exist "..\venv\Scripts\python.exe" set "PY=..\venv\Scripts\python.exe"
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if defined VENV if exist "%VENV%\Scripts\python.exe" set "PY=%VENV%\Scripts\python.exe"
set "TESTDIR=%CD%\tests"
if defined RUN_TESTS_DIR set "TESTDIR=%RUN_TESTS_DIR%"
set /a passed=0, failed=0
if "%~1"=="" goto all
if /i "%~1"=="style" goto style
:named
if "%~1"=="" goto summary
set "SUITE=%~nx1"
if not "!SUITE:~0,5!"=="test_" set "SUITE=test_!SUITE!"
if /i not "!SUITE:~-3!"==".py" set "SUITE=!SUITE!.py"
call :run "%TESTDIR%\!SUITE!"
shift
goto named
:style
for %%S in (welcome_chatter_for_info autoreply humanize_typos ask_pacing nonnative_style send_followup send_welcome) do call :run "%TESTDIR%\test_%%S.py"
goto summary
:all
for %%F in ("%TESTDIR%\test_*.py") do call :run "%%~fF"
goto summary
:run
set "RESULT=%TEMP%\twin-suite-%RANDOM%-%RANDOM%.txt"
if not exist "%~1" (
  echo [FAIL] %~nx1: suite not found
  set /a failed+=1
  exit /b 0
)
"%PY%" "%~1" >"!RESULT!" 2>&1
set "PYTHON_EXIT=!ERRORLEVEL!"
findstr /c:"all cases passed" "!RESULT!" >nul 2>&1
set "MARKER_EXIT=!ERRORLEVEL!"
if "!PYTHON_EXIT!"=="0" if "!MARKER_EXIT!"=="0" (
  echo [PASS] %~nx1
  set /a passed+=1
  del "!RESULT!" >nul 2>&1
  exit /b 0
)
echo [FAIL] %~nx1: python exit !PYTHON_EXIT!; required marker: all cases passed
type "!RESULT!"
del "!RESULT!" >nul 2>&1
set /a failed+=1
exit /b 0
:summary
echo !passed! passed, !failed! failed
set "RUNNER_EXIT=1"
if "!failed!"=="0" (
  echo all backend suites passed
  set "RUNNER_EXIT=0"
) else (
  echo SOME TESTS FAILED
)
popd
exit /b !RUNNER_EXIT!
