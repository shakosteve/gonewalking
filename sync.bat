@echo off
rem GoneWalking scheduled update: sync new walks, match photos, log it and e-mail a report.
rem For Windows Task Scheduler: Program = this file (no arguments, no "Start in" needed).
rem Settings (report e-mail etc.) are in .env; see scheduled_sync.py for the list.
cd /d "%~dp0"
"C:\Users\zamdr\AppData\Local\Programs\Python\Python313\python.exe" scheduled_sync.py %*
exit /b %ERRORLEVEL%
