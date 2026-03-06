# Scratch House (Prototype)

`codex.md` 초안을 기반으로 만든 최소 동작 구현입니다.

## 구현된 범위
- WebSocket Signaling/State 서버
- `aiortc` 기반 WebRTC 음악 브로드캐스트 PoC (`/debug/music-poc`)
- 라운지 유저 상태 동기화 (입장/퇴장, mute/speaking)
- 음악 상태 동기화 (`/play`, `/pause`, `/resume`, `/seek`)
- 음악 큐 동기화 (`/play` enqueue, host의 `/next`로만 다음 곡)
- `/skip` 요청 알람 (일반 유저 -> host)
- Telegram `/link` 버튼 기반 세션 연결
- 보드 상태 동기화 (`/board ...`)
- 접속 시간 기반 랭킹 브로드캐스트 (5초 주기)
- 터미널 클라이언트 명령어 인터페이스
- `tui.md` 스타일 기반 실시간 대시보드 TUI
- `Token Usage`를 Claude Code 사용 로그(`~/.claude/projects/...`) 기반으로 표시
- 선택적 로컬 `mpv` 실행 (`--mpv`)
- `yt-dlp -> direct audio URL -> mpv` 재생 파이프라인
- `mpv` IPC(JSON socket) 기반 pause/resume/seek 동기화

## 설치
```bash
python3.8 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e .
```

WebRTC 음악 PoC까지 설치하려면:
```bash
pip install -e ".[webrtc]"
```

`aiortc` extra는 Python 3.10 이상에서만 설치를 권장합니다.

## 실행
1) 서버
```bash
scratch-house-server --host 0.0.0.0 --port 8765 --api-host 127.0.0.1 --api-port 8787 --public-api-base http://127.0.0.1:8787 --link-api-token <SECRET_TOKEN> --reports-dir reports
```

또는 실행 스크립트:
```bash
chmod +x ./run_server.sh
LINK_API_TOKEN=<SECRET_TOKEN> ./run_server.sh
```

문제 진단 모드:
```bash
DEBUG=1 ./run_server.sh
```

포트/바인드 주소 변경:
```bash
WS_HOST=0.0.0.0 WS_PORT=8765 API_BIND_HOST=127.0.0.1 API_BIND_PORT=8787 ./run_server.sh
```

WebRTC 음악 PoC 확인:
```text
http://127.0.0.1:8787/debug/music-poc
```

PoC 페이지에서 `Start Receiver`를 누르면 서버가 WebRTC 오디오 트랙을 발행합니다.
현재 곡 상태가 `playing=true`이면 FFmpeg가 생성한 톤이 들리고, pause/idle이면 FFmpeg 무음 소스로 전환됩니다.

2) Telegram bot
```bash
scratch-house-telegram-bot --bot-token <TELEGRAM_BOT_TOKEN> --link-api-base http://127.0.0.1:8787 --link-api-token <SECRET_TOKEN>
```

3) 클라이언트 A (Telegram 링크 모드)
```bash
scratch-house-client --server ws://127.0.0.1:8765 --link-telegram --device-name alice-macbook
```

4) 클라이언트 B (수동 이름 모드)
```bash
scratch-house-client --name bob --server ws://127.0.0.1:8765 --webrtc-api-base http://127.0.0.1:8787 --claude-project-path /Users/you/sideproj/hyperfocus_cli
```

`mpv`를 실제로 붙이고 싶으면:
```bash
scratch-house-client --name alice --server ws://127.0.0.1:8765 --disable-webrtc-recv --mpv
```

`yt-dlp` 추출을 끄고 원본 URL을 직접 재생하려면:
```bash
scratch-house-client --name alice --server ws://127.0.0.1:8765 --mpv --disable-yt-dlp
```

## 클라이언트 명령어
- 공통: `/help`, `/play <url>`, `/mute on|off`, `/users`, `/ranking`, `/queue`, `/quit`
- Host only: `/pause`, `/resume`, `/seek <seconds>`, `/next`, `/close`, `/board <text>`, `/speak on|off`
- 일반 유저의 `/play <url>` 는 즉시 재생이 아니라 대기 큐에만 추가됩니다.

## TUI 단축키
- `Q`: 종료 (`/quit`)
- `W`: Work 상태 (`mute off + speaking on`)
- `R`: Rest 상태 (`speaking off`)
- `M`: Mute 토글
- `N`: 다음 곡 (`/next`, host only)
- `X`: 세션 종료 (`/close`, host only)

`members`와 `leaderboard`는 서버의 `user_state`/`ranking_state` 이벤트 수신 시 자동 리프레시됩니다.
`Token Usage`는 각 클라이언트가 로컬 Claude Code 로그를 읽어 서버에 주기 보고한 값을 사용합니다.
클라이언트의 `mpv` 직접재생 경로는 아직 남아 있는 레거시 fallback이며, 새 음악 경로 검증은 서버 WebRTC PoC 기준으로 진행합니다.

## Telegram 링크 플로우
1. 사용자가 `scratch-house-client --link-telegram` 실행
2. 클라이언트가 pending 세션으로 대기
3. 텔레그램 채팅방에서 `/link`
4. 봇이 세션 버튼 목록 표시
5. 사용자가 자신의 세션 버튼 클릭
6. 서버가 해당 세션을 Telegram 사용자로 인증하고 입장 완료

## 구조
- `scratch_house/server.py`: Signaling + 상태 브로드캐스트
- `scratch_house/client.py`: 터미널 인터랙션 + 상태 렌더링
- `scratch_house/models.py`: 공용 상태 모델

## 메모
- 현재 WebRTC PoC는 `aiortc` + `FFmpeg stdout PCM` 기반 서버 발행 오디오 트랙만 검증합니다.
- 현재 서버는 `yt-dlp`로 유튜브 오디오 스트림 URL을 해석한 뒤, `FFmpeg`가 이를 PCM으로 디코딩해 WebRTC 트랙으로 송출합니다.
- WebRTC 수신 클라이언트는 `aiortc + ffplay` 기반이며, `--webrtc-api-base` 또는 서버의 `--public-api-base`가 필요합니다.
- 실제 WebRTC 음성(SFU/Opus 튜닝)과 브라우저 외 수신 클라이언트는 다음 단계 구현 대상입니다.
- 현재는 문서의 상태 동기화/라운지 동작을 우선 검증하는 프로토타입입니다.
- `mpv` IPC는 Unix 계열(macOS/Linux)에서 활성화되며, Windows에서는 제한적으로 동작합니다.
- host는 첫 `/play`를 보낸 사용자로 설정되며, host 이탈 시 접속 중 가장 오래된 사용자로 자동 승계됩니다.
- non-host 유저는 서버 기준으로 `/play` 큐 추가와 `/mute`만 허용됩니다.
- host가 `/close`를 실행하면 모든 세션을 하드킬하고 `--reports-dir`에 결산 JSON 파일을 저장합니다.
- Link API는 `--link-api-token` 설정 시 Bearer 인증이 필요합니다.
