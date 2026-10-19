@echo off
chcp 65001 >nul
title Toxic Skill Club - бот бронирования
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Готовлю окружение Python...
    %PY% -m venv .venv
    if errorlevel 1 goto nopython
)

echo [2/3] Проверяю библиотеки...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed

echo [3/3] Запускаю бота...
".venv\Scripts\python.exe" bot.py %*
echo.
echo Бот остановлен.
pause
exit /b 0

:nopython
echo.
echo Не найден Python. Скачайте Python 3.12 с https://www.python.org/downloads/
echo При установке поставьте галочку "Add python.exe to PATH", затем запустите start.bat снова.
pause
exit /b 1

:failed
echo.
echo Не удалось установить библиотеки. Проверьте интернет и запустите start.bat снова.
pause
exit /b 1
