# 기차표 자동예매 (SRT / KTX)

매진된 기차표의 빈자리를 반복 조회해서 자리가 나면 즉시 예매(선택 시 자동결제)하고 텔레그램으로 알려주는 웹 프로그램입니다.

## 새 PC에서 시작하기

1. Python 3.10 이상 설치 (설치 시 **Add Python to PATH** 체크) — https://www.python.org/downloads/
2. 이 저장소 받기
   ```
   git clone <저장소 주소>
   ```
   (git이 없으면 GitHub 페이지의 **Code → Download ZIP** 으로 받아 압축 해제)
3. `run.bat` 더블클릭 (macOS/Linux는 `./run.sh`)
   - 처음 실행하면 `config.json`이 만들어지고 메모장이 열립니다. 아래 값을 채우고 저장하세요.
   - 필요한 라이브러리가 자동 설치되고 브라우저에 http://localhost:5000 이 열립니다.

## config.json

| 키 | 내용 |
|---|---|
| `telegram_token` | 텔레그램 봇 토큰 (BotFather에서 발급) |
| `telegram_chat_id` | 알림 받을 채팅 ID |
| `user_id` | SRT/코레일 로그인 전화번호 (예: 01012345678) |
| `password` | SRT/코레일 비밀번호 |

`config.json`은 `.gitignore`에 포함되어 있어 GitHub에 올라가지 않습니다. PC마다 한 번씩만 입력하면 됩니다.
환경변수 `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TRAIN_USER_ID`, `TRAIN_PASSWORD`로도 설정할 수 있습니다.
카드 정보는 브라우저 localStorage에만 저장되며 서버·파일에 기록되지 않습니다.

## 구조

| 파일 | 역할 |
|---|---|
| `app.py` | Flask 서버 + 내장 웹 UI, 예매 루프 |
| `SRT/` | SRT 비공식 라이브러리(ryanking13/SRT)를 NetFunnel 503 패치 적용해 내장한 것. pip로 별도 설치 불필요 |
| `requirements.txt` | flask, requests, beautifulsoup4, korail2 |
| `run.bat` / `run.sh` | 설치 + 실행 스크립트 |

## 주의

- 비공식 라이브러리를 사용하므로 SRT/코레일 사이트가 바뀌면 동작이 멈출 수 있습니다.
- 조회 간격을 너무 짧게 하면 계정이 일시 차단될 수 있습니다.
