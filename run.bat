@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1 || (echo [오류] Python이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치 시 "Add to PATH" 체크 후 다시 실행하세요. & pause & exit /b 1)

if not exist config.json (
  copy config.example.json config.json >nul
  echo [안내] config.json 을 만들었습니다. 메모장이 열리면 텔레그램 토큰/전화번호/비밀번호를 입력하고 저장한 뒤 창을 닫으세요.
  notepad config.json
)

taskkill /IM python.exe /F >nul 2>&1
timeout /t 1 /nobreak >nul
pip install -r requirements.txt --quiet --disable-pip-version-check
start "" /min python app.py
timeout /t 4 /nobreak >nul
start "" "http://localhost:5000"
