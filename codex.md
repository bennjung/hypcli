# 🎧 Scratch house - Tech Stack & Architecture (Draft)

## 📌 Project Overview
- **Concept:** 온라인 작업자(주로 개발자/크리에이터)를 위한 초경량 CLI/TUI 기반 프라이빗 음성 채팅 & 음악 라운지.
- **Target Environment:** 프라이빗 홈 서버 (Self-Hosted) + 데스크톱 터미널 클라이언트 (Windows/Mac/Linux).
- **Core Value:** 프론트엔드 렌더링 자원을 배제한 극강의 가벼움, 서버 중심 WebRTC 오디오 파이프라인 기반의 초고음질 공유 청취 경험, 텍스트 기반의 해커 감성 UI.

---

## 🏗️ System Architecture 

본 프로젝트는 **음성 채팅과 음악 재생 모두를 중앙 WebRTC 미디어 서버가 중계하는 서버 중심 구조**를 채택합니다.  
음악은 더 이상 각 클라이언트가 유튜브를 직접 재생하지 않고, 서버가 `yt-dlp`와 `FFmpeg`를 사용해 오디오를 추출/변환한 뒤 WebRTC로 방 전체에 브로드캐스트합니다.

### 1. Client-Side (Terminal User Interface)
터미널 환경에서 시각적 요소와 백그라운드 오디오/마이크/WebRTC 제어를 동시에 수행합니다. 단일 실행 파일(Binary) 배포를 목표로 합니다.

* **UI Framework (TUI):** * *Option A (Python):* `Textual` 또는 `Rich` (화려한 컴포넌트, 빠른 프로토타이핑)
    * *Option B (Go):* `Bubble Tea` (압도적으로 가볍고 빠른 실행, 깔끔한 동시성 처리)
* **Voice / Music Engine (WebRTC):** * *Option A (Python):* `aiortc` + `PyAudio` 또는 시스템 오디오 출력
    * *Option B (Go):* `pion/webrtc`
    * 서버가 보내는 마이크 오디오와 음악 오디오를 각각 구독.
    * 음악은 방 안의 **가상 DJ 유저**가 송출하는 트랙처럼 수신.
    * **Tuning:** Opus 코덱 비트레이트 제한 해제 (음악 채널 256~510kbps 목표), 음성 채널은 AGC/Echo Cancellation 선택 적용.
* **Local Playback Responsibility**
    * 클라이언트는 유튜브 URL을 직접 열지 않음.
    * 클라이언트는 서버가 만든 WebRTC 수신 트랙만 디코딩/출력.
    * 재생/일시정지/탐색은 모두 서버 authoritative 상태를 따름.

### 2. Server-Side (Home Server)
오디오 트래픽 라우팅과 유저 간의 상태 동기화(Signaling)만 담당하여 가벼운 홈 서버에서도 무리 없이 구동됩니다.

* **Media Server (WebRTC SFU):** `Mediasoup` (Node.js/Rust) 또는 `LiveKit` (Go)
    * P2P 방식의 한계를 극복하고, 여러 명의 마이크 오디오 트래픽을 효율적으로 중계.
    * 음악용 서버 발행 트랙을 모든 참가자에게 fan-out.
    * 방 입장 시 클라이언트는 일반 유저 마이크 트랙과 별도로 `music-bot` 오디오 트랙을 subscribe.
* **Signaling & State Server:** `WebSocket` (Socket.io 또는 Gorilla WebSocket)
    * 빠르고 지연 없는 양방향 통신으로 아래의 상태를 동기화.
    * *Music State:* 현재 재생 곡 URL, 큐 상태, 파이프라인 상태, 가상 DJ 세션 메타데이터 브로드캐스팅.
    * *User State:* 라운지 입장/퇴장, 마이크 ON/OFF 상태 표출.
    * *Board State:* 프로젝트 현황 텍스트 데이터 동기화.
* **Music Ingest Pipeline**
    * 유저가 `/play [URL]`로 곡 예약.
    * 서버의 `yt-dlp`가 유튜브에서 최적 오디오 스트림 주소를 추출.
    * 서버 백그라운드의 `FFmpeg`가 해당 스트림을 읽어 `Opus` 기반 WebRTC 송출용 포맷으로 실시간 인코딩.
    * SFU가 이를 방의 모든 참가자에게 브로드캐스트.
    * 구현 관점에서는 **"음악을 틀어주는 가상의 유저가 방에 입장한 것처럼"** 취급.

---

## 🛠️ Feature Implementation Details

| Feature | Implementation Logic |
| :--- | :--- |
| **Lounge (UI)** | 원형 테이블 대신 TUI 패널을 분할. 좌측 패널에 유저 리스트 출력. 마이크 입력 감지 시 닉네임 옆 `[🔊]` 아이콘 점등 (WebRTC 오디오 레벨 미터 활용). |
| **Music Broadcast** | 1. 유저가 TUI 커맨드로 곡 예약 (`/play [URL]`).<br>2. 서버가 큐 등록 후 `yt-dlp`로 오디오 스트림 URL 확보.<br>3. 서버가 `FFmpeg`로 실시간 `Opus` 인코딩(256~510kbps 목표).<br>4. SFU가 이를 `music-bot` 발행 트랙으로 모든 참가자에게 브로드캐스트.<br>5. 클라이언트는 별도 동기화 재생이 아니라 WebRTC 수신 스트림을 그대로 출력. |
| **Bullet Board** | 우측 상단 TUI 패널. 서버의 JSON/Markdown 데이터를 웹소켓으로 받아 렌더링. 터미널 단축키로 텍스트 수정 및 서버 전송. |
| **Ranking** | 우측 하단 TUI 패널. 서버에서 웹소켓 연결 유지 시간(Active Time)을 누적 계산하여 ASCII Art 바 차트나 리스트 형태로 리더보드 출력. |

---

## 🚀 Deployment Strategy
* **Server:** 홈 서버(Linux/Docker)에 `Media Server`, `Signaling Server`, 그리고 `yt-dlp`/`FFmpeg` 실행 환경을 함께 배포. DDNS 및 포트 포워딩으로 외부 접속 허용.
* **Client:** Python(PyInstaller) 또는 Go(go build)를 이용해 OS별(Windows/Mac) 독립 실행형 바이너리로 컴파일하여 지인들에게 배포.
* **Operational Model:** 음악 재생 권한과 파이프라인 상태는 서버가 단일 권한을 가지며, 클라이언트는 예약/제어 명령만 전송.
