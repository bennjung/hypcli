# 🎧 Scratch house - Tech Stack & Architecture (Draft)

## 📌 Project Overview
- **Concept:** 온라인 작업자(주로 개발자/크리에이터)를 위한 초경량 CLI/TUI 기반 프라이빗 음성 채팅 & 음악 라운지.
- **Target Environment:** 프라이빗 홈 서버 (Self-Hosted) + 데스크톱 터미널 클라이언트 (Windows/Mac/Linux).
- **Core Value:** 프론트엔드 렌더링 자원을 배제한 극강의 가벼움, 디스코드를 뛰어넘는 무손실 고음질, 텍스트 기반의 힙한 해커 감성 UI.

---

## 🏗️ System Architecture 

본 프로젝트는 홈 서버의 트래픽 부하를 최소화하기 위해 **음성 채팅은 중앙 서버(SFU)를 거치고, 음악 재생은 클라이언트 단에서 구글 서버로부터 직접 스트리밍(Client-side Sync)하는 하이브리드 구조**를 채택합니다.

### 1. Client-Side (Terminal User Interface)
터미널 환경에서 시각적 요소와 백그라운드 오디오/마이크 제어를 동시에 수행합니다. 단일 실행 파일(Binary) 배포를 목표로 합니다.

* **UI Framework (TUI):** * *Option A (Python):* `Textual` 또는 `Rich` (화려한 컴포넌트, 빠른 프로토타이핑)
    * *Option B (Go):* `Bubble Tea` (압도적으로 가볍고 빠른 실행, 깔끔한 동시성 처리)
* **Music Player Engine:** `mpv` (CLI 백그라운드 구동)
    * 내장된 `yt-dlp`를 통해 유튜브 URL에서 광고 없는 순수 오디오 스트림(Direct URL)만 추출하여 다이렉트 재생.
    * IPC(소켓 통신)를 통해 서버로부터 받은 Sync(재생/일시정지/탐색) 명령을 0.1초 단위로 수행.
* **Voice Engine (WebRTC):** * *Option A (Python):* `aiortc` + `PyAudio` (마이크 제어)
    * *Option B (Go):* `pion/webrtc`
    * **Tuning:** Opus 코덱 비트레이트 제한 해제 (최대 510kbps), 오디오 필터(AGC, Echo Cancellation) 선택적 적용으로 디스코드 대비 압도적 고음질 확보.

### 2. Server-Side (Home Server)
오디오 트래픽 라우팅과 유저 간의 상태 동기화(Signaling)만 담당하여 가벼운 홈 서버에서도 무리 없이 구동됩니다.

* **Media Server (WebRTC SFU):** `Mediasoup` (Node.js/Rust) 또는 `LiveKit` (Go)
    * P2P 방식의 한계를 극복하고, 여러 명의 마이크 오디오 트래픽을 효율적으로 중계.
* **Signaling & State Server:** `WebSocket` (Socket.io 또는 Gorilla WebSocket)
    * 빠르고 지연 없는 양방향 통신으로 아래의 상태를 동기화.
    * *Music State:* 현재 재생 곡 URL, 타임스탬프 브로드캐스팅.
    * *User State:* 라운지 입장/퇴장, 마이크 ON/OFF 상태 표출.
    * *Board State:* 프로젝트 현황 텍스트 데이터 동기화.

---

## 🛠️ Feature Implementation Details

| Feature | Implementation Logic |
| :--- | :--- |
| **Lounge (UI)** | 원형 테이블 대신 TUI 패널을 분할. 좌측 패널에 유저 리스트 출력. 마이크 입력 감지 시 닉네임 옆 `[🔊]` 아이콘 점등 (WebRTC 오디오 레벨 미터 활용). |
| **Music Sync** | 1. 유저가 TUI 커맨드로 곡 예약 (`/play [URL]`).<br>2. 서버가 큐(Queue)에 등록 후 현재 재생 시간 브로드캐스팅.<br>3. 클라이언트의 `mpv`가 백그라운드에서 스트림 직출 및 싱크에 맞춰 재생. |
| **Bullet Board** | 우측 상단 TUI 패널. 서버의 JSON/Markdown 데이터를 웹소켓으로 받아 렌더링. 터미널 단축키로 텍스트 수정 및 서버 전송. |
| **Ranking** | 우측 하단 TUI 패널. 서버에서 웹소켓 연결 유지 시간(Active Time)을 누적 계산하여 ASCII Art 바 차트나 리스트 형태로 리더보드 출력. |

---

## 🚀 Deployment Strategy
* **Server:** 홈 서버(Linux/Docker)에 `Media Server`와 `Signaling Server` 컨테이너 배포. DDNS 및 포트 포워딩으로 외부 접속 허용.
* **Client:** Python(PyInstaller) 또는 Go(go build)를 이용해 OS별(Windows/Mac) 독립 실행형 바이너리로 컴파일하여 지인들에게 배포.
