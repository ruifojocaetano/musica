@echo off
rem Serve a pagina so neste PC (127.0.0.1) para o YouTube aceitar o leitor, e abre-a no browser.
start "" pythonw "%~dp0servir.pyw"
timeout /t 1 /nobreak >nul
start "" "http://localhost:8765/index.html"
