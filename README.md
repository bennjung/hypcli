# Scratch House (Prototype)

`codex.md` 초안을 기반으로 만든 최소 동작 구현입니다.

## 구현된 범위
- WebSocket Signaling/State 서버
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
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 실행
1) 서버
```bash
scratch-house-server --host 0.0.0.0 --port 8765 --api-host 127.0.0.1 --api-port 8787 --link-api-token <SECRET_TOKEN> --reports-dir reports
```

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
scratch-house-client --name bob --server ws://127.0.0.1:8765 --claude-project-path /Users/you/sideproj/hyperfocus_cli
```

`mpv`를 실제로 붙이고 싶으면:
```bash
scratch-house-client --name alice --server ws://127.0.0.1:8765 --mpv
```

`yt-dlp` 추출을 끄고 원본 URL을 직접 재생하려면:
```bash
scratch-house-client --name alice --server ws://127.0.0.1:8765 --mpv --disable-yt-dlp
```

## 클라이언트 명령어
- `/help`
- `/play <url>`
- `/skip`
- `/pause`
- `/resume`
- `/seek <seconds>`
- `/next` (host only)
- `/close` (host only, 모든 세션 종료 + 결산 리포트 저장)
- `/board <text>`
- `/mute on|off`
- `/speak on|off`
- `/users`
- `/ranking`
- `/queue`
- `/quit`

## TUI 단축키
- `Q`: 종료 (`/quit`)
- `W`: Work 상태 (`mute off + speaking on`)
- `R`: Rest 상태 (`speaking off`)
- `M`: Mute 토글
- `N`: 다음 곡 (`/next`, host only)
- `X`: 세션 종료 (`/close`, host only)

`members`와 `leaderboard`는 서버의 `user_state`/`ranking_state` 이벤트 수신 시 자동 리프레시됩니다.
`Token Usage`는 각 클라이언트가 로컬 Claude Code 로그를 읽어 서버에 주기 보고한 값을 사용합니다.
`yt-dlp`가 설치되어 있으면 URL에서 오디오 직링크를 추출해 `mpv`로 재생합니다. 미설치 시 원본 URL 재생으로 fallback 됩니다.

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
- 실제 WebRTC 음성(SFU/Opus 튜닝)은 다음 단계 구현 대상으로 남겨두었습니다.
- 현재는 문서의 상태 동기화/라운지 동작을 우선 검증하는 프로토타입입니다.
- `mpv` IPC는 Unix 계열(macOS/Linux)에서 활성화되며, Windows에서는 제한적으로 동작합니다.
- host는 첫 `/play`를 보낸 사용자로 설정되며, host 이탈 시 접속 중 가장 오래된 사용자로 자동 승계됩니다.
- host가 `/close`를 실행하면 모든 세션을 하드킬하고 `--reports-dir`에 결산 JSON 파일을 저장합니다.
- Link API는 `--link-api-token` 설정 시 Bearer 인증이 필요합니다.
