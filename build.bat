@echo off
chcp 65001 >nul
echo ============================================
echo   Binance Auto Trading Bot - Build Script
echo   Korean + English dual build
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python이 설치되지 않았습니다.
    echo https://www.python.org/downloads/ 에서 설치하세요.
    pause
    exit /b 1
)

:: Install dependencies
echo [1/5] 의존성 설치 중...
pip install -r requirements.txt --quiet
pip install pyinstaller --quiet
echo       완료

:: Clean previous build
echo [2/5] 이전 빌드 정리 중...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
echo       완료

:: ── Korean Build ──
echo [3/5] 한국어 exe 빌드 중... (2-5분 소요)
copy /y gui_config.template.json gui_config.json >nul
pyinstaller bot.spec --noconfirm
if exist dist\BinanceAutoBot.exe (
    echo       한국어 빌드 성공!
    :: 배포 폴더 구성
    if not exist dist\assets mkdir dist\assets
    copy /y assets\* dist\assets\ >nul
    copy /y gui_config.template.json dist\gui_config.json >nul
    copy /y README.md dist\ >nul 2>nul
    :: 한국어 exe 이름 변경
    rename dist\BinanceAutoBot.exe BinanceAutoBot_KR.exe
) else (
    echo [ERROR] 한국어 빌드 실패.
    pause
    exit /b 1
)

:: Clean build cache (keep dist)
if exist build rmdir /s /q build

:: ── English Build ──
echo [4/5] 영문 exe 빌드 중... (2-5분 소요)
copy /y gui_config.template.en.json gui_config.json >nul
pyinstaller bot.spec --noconfirm
if exist dist\BinanceAutoBot.exe (
    echo       영문 빌드 성공!
    :: 영문 exe 이름 변경
    rename dist\BinanceAutoBot.exe BinanceAutoBot_EN.exe
    :: gui_config를 한국어 기본으로 복원
    copy /y gui_config.template.json dist\gui_config.json >nul
) else (
    echo [ERROR] 영문 빌드 실패.
    pause
    exit /b 1
)

echo.
echo [5/5] 빌드 완료!
echo ============================================
echo   배포 폴더: dist\
echo   - BinanceAutoBot_KR.exe  (한국어 기본)
echo   - BinanceAutoBot_EN.exe  (English default)
echo ============================================
echo.
pause
