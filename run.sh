#!/usr/bin/env bash
# macOS / Linux 실행용
cd "$(dirname "$0")"
[ -f config.json ] || { cp config.example.json config.json; echo "config.json 을 만들었습니다. 내용을 수정한 뒤 다시 실행하세요."; exit 0; }
pip install -r requirements.txt --quiet
python3 app.py
