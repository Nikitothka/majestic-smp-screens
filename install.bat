@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Установка — разбор скриншотов СМП

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Не найден Python. Скачай Python 3.11 или новее с https://www.python.org/downloads/
  echo  При установке поставь галочку "Add python.exe to PATH", потом запусти этот файл снова.
  echo.
  pause
  exit /b 1
)

echo  Создаю отдельное окружение в папке .venv ...
python -m venv .venv || goto :fail
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul

echo.
set /p GPU= Есть видеокарта NVIDIA? Будет быстрее, но скачается ~2.5 ГБ. [y/N]: 
if /i "%GPU%"=="y" (
  echo  Ставлю torch под видеокарту NVIDIA ...
  python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128 || goto :fail
) else (
  echo  Ставлю torch под процессор ...
  python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu || goto :fail
)

echo  Ставлю остальное ...
python -m pip install -r requirements.txt || goto :fail

echo.
echo  Готово. Запускай run.bat
echo  При первом разборе программа ещё скачает модели распознавания ~150 МБ.
pause
exit /b 0

:fail
echo.
echo  Установка не удалась. Скопируй текст выше и отправь тому, кто дал программу.
pause
exit /b 1
