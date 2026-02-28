@echo off
chcp 65001 >nul
echo ============================================
echo   Binance Auto Trading Bot - Build Script
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
echo [1/4] 의존성 설치 중...
pip install -r requirements.txt --quiet
pip install pyinstaller --quiet
echo       완료

:: Clean previous build
echo [2/4] 이전 빌드 정리 중...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
echo       완료

:: Copy template config
echo [3/4] 배포용 설정 파일 준비 중...
copy /y gui_config.template.json gui_config.json >nul
echo       완료

:: Build
echo [4/4] exe 빌드 중... (2-5분 소요)
pyinstaller bot.spec --noconfirm
echo.

if exist dist\BinanceAutoBot.exe (
    echo ============================================
    echo   빌드 성공!
    echo   실행 파일: dist\BinanceAutoBot.exe
    echo ============================================

    :: Copy assets to dist
    if not exist dist\assets mkdir dist\assets
    copy /y assets\* dist\assets\ >nul
    copy /y gui_config.template.json dist\gui_config.json >nul
    copy /y README.md dist\ >nul 2>nul

    echo.
    echo   배포 폴더: dist\
    echo   레퍼럴 코드 설정: dist\gui_config.json의
    echo   "binance_referral_code" 값을 수정하세요.
) else (
    echo [ERROR] 빌드 실패. 위 에러 메시지를 확인하세요.
)

echo.
pause
