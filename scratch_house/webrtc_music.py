from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any
from uuid import uuid4

from aiohttp import web

LOG = logging.getLogger("scratch_house.webrtc")

try:
    from av import AudioFrame
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaRelay
    from aiortc.mediastreams import MediaStreamTrack

    AIORTC_AVAILABLE = True
    AIORTC_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
    AudioFrame = None  # type: ignore[assignment]
    RTCPeerConnection = None  # type: ignore[assignment]
    RTCSessionDescription = None  # type: ignore[assignment]
    MediaRelay = None  # type: ignore[assignment]
    MediaStreamTrack = object  # type: ignore[assignment]
    AIORTC_AVAILABLE = False
    AIORTC_IMPORT_ERROR = str(exc)


class YtDlpResolver:
    def __init__(self) -> None:
        self.binary = shutil.which("yt-dlp")
        self._cache: dict[str, tuple[str, float]] = {}
        self.cache_ttl_seconds = 600

    @property
    def available(self) -> bool:
        return bool(self.binary)

    async def resolve(self, source_url: str) -> str:
        source = source_url.strip()
        if not source:
            return source
        if not self._looks_like_url(source) or not self.binary:
            return source

        cached = self._cache.get(source)
        if cached and cached[1] > time.time():
            return cached[0]

        resolved = await asyncio.to_thread(self._resolve_blocking, source)
        if resolved and resolved != source:
            self._cache[source] = (resolved, time.time() + self.cache_ttl_seconds)
        return resolved or source

    def _resolve_blocking(self, source_url: str) -> str | None:
        if not self.binary:
            return None

        command = [
            self.binary,
            "--no-playlist",
            "-f",
            "bestaudio",
            "--get-url",
            "--no-warnings",
            "--",
            source_url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception:
            LOG.warning("yt-dlp execution failed for %s", source_url, exc_info=True)
            return None

        if completed.returncode != 0:
            LOG.warning("yt-dlp could not resolve %s", source_url)
            return None

        for line in completed.stdout.splitlines():
            value = line.strip()
            if value:
                return value
        return None

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        lowered = value.lower()
        return lowered.startswith("http://") or lowered.startswith("https://")


@dataclass
class PlaybackConfig:
    source_url: str = ""
    position_seconds: float = 0.0
    playing: bool = False
    generation: int = 0


class FfmpegPcmSource:
    def __init__(self, sample_rate: int = 48_000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = int(sample_rate * 0.02)
        self.frame_bytes = self.frame_samples * channels * 2
        self.binary = shutil.which("ffmpeg")
        self._process: subprocess.Popen[bytes] | None = None
        self._current_mode = ""
        self._process_generation = -1
        self._config = PlaybackConfig()
        self._started_monotonic = 0.0
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self.binary)

    @property
    def current_mode(self) -> str:
        return self._current_mode or "stopped"

    async def set_stream(self, source_url: str, position_seconds: float, playing: bool) -> None:
        async with self._lock:
            self._config = PlaybackConfig(
                source_url=source_url.strip(),
                position_seconds=max(0.0, float(position_seconds)),
                playing=bool(playing),
                generation=self._config.generation + 1,
            )
            self._restart_process()

    async def clear(self) -> None:
        await self.set_stream(source_url="", position_seconds=0.0, playing=False)

    async def read_frame(self) -> bytes:
        if not self.available:
            raise RuntimeError("ffmpeg is not installed")

        async with self._lock:
            await self._ensure_process()
            process = self._process
            if not process or not process.stdout:
                raise RuntimeError("ffmpeg process is not available")

            chunk = bytearray()
            while len(chunk) < self.frame_bytes:
                remaining = self.frame_bytes - len(chunk)
                part = await asyncio.to_thread(process.stdout.read, remaining)
                if not part:
                    break
                chunk.extend(part)
            if len(chunk) == self.frame_bytes:
                return bytes(chunk)

            if process.poll() is None:
                LOG.warning("Short FFmpeg PCM read (%d bytes). restarting source.", len(chunk))
            self._restart_process()
            return b"\x00" * self.frame_bytes

    async def shutdown(self) -> None:
        async with self._lock:
            self._stop_process()

    async def _ensure_process(self) -> None:
        process_dead = self._process is None or self._process.poll() is not None
        generation_changed = self._process_generation != self._config.generation
        if process_dead or generation_changed:
            self._restart_process()

    def _restart_process(self) -> None:
        self._stop_process()
        self._process = self._spawn_process()
        self._process_generation = self._config.generation
        self._started_monotonic = time.monotonic()

    def _spawn_process(self) -> subprocess.Popen[bytes]:
        if not self.binary:
            raise RuntimeError("ffmpeg is not installed")

        mode = self._target_mode()
        command = [
            self.binary,
            "-hide_banner",
            "-loglevel",
            "error",
        ]

        if mode == "stream":
            start_position = max(0.0, self._restart_position())
            if start_position > 0:
                command.extend(["-ss", f"{start_position:.3f}"])
            command.extend(["-i", self._config.source_url])
        else:
            command.extend([
                "-f",
                "lavfi",
                "-i",
                self._input_filter(mode),
            ])

        command.extend(
            [
                "-vn",
                "-ac",
                str(self.channels),
                "-ar",
                str(self.sample_rate),
                "-f",
                "s16le",
                "pipe:1",
            ]
        )
        LOG.info("Starting FFmpeg PCM source mode=%s url=%s", mode, self._config.source_url or "-")
        self._current_mode = mode
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

    def _restart_position(self) -> float:
        if not self._config.playing:
            return self._config.position_seconds
        elapsed = max(0.0, time.monotonic() - self._started_monotonic)
        return self._config.position_seconds + elapsed

    def _target_mode(self) -> str:
        if self._config.playing and self._config.source_url:
            return "stream"
        return "silence"

    def _input_filter(self, mode: str) -> str:
        if mode == "tone":
            return f"sine=frequency=440:sample_rate={self.sample_rate}"
        return f"anullsrc=r={self.sample_rate}:cl=mono"

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        self._current_mode = ""
        self._process_generation = -1
        if not process:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if process.stdout:
            process.stdout.close()


class FfmpegAudioStreamTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source: FfmpegPcmSource) -> None:
        super().__init__()
        self.source = source
        self.sample_rate = source.sample_rate
        self.frame_samples = source.frame_samples
        self._pts = 0

    async def recv(self) -> Any:
        pcm_bytes = await self.source.read_frame()
        frame = AudioFrame(format="s16", layout="mono", samples=self.frame_samples)
        frame.planes[0].update(pcm_bytes)
        frame.sample_rate = self.sample_rate
        frame.time_base = Fraction(1, self.sample_rate)
        frame.pts = self._pts
        self._pts += self.frame_samples
        return frame


class AiortcMusicPoc:
    def __init__(self) -> None:
        self._peer_connections: set[Any] = set()
        self._pcm_source: FfmpegPcmSource | None = None
        self._source_track: Any | None = None
        self._relay: Any | None = None
        self._resolver = YtDlpResolver()
        self._playback_lock = asyncio.Lock()
        self._requested_url = ""
        self._stream_url = ""
        self._position_seconds = 0.0
        self._playing = False
        self._last_error = ""

        if AIORTC_AVAILABLE:
            self._pcm_source = FfmpegPcmSource()
            self._source_track = FfmpegAudioStreamTrack(source=self._pcm_source)
            self._relay = MediaRelay()

    @property
    def enabled(self) -> bool:
        return AIORTC_AVAILABLE and bool(self._pcm_source and self._pcm_source.available)

    async def load_track(self, source_url: str, position_seconds: float = 0.0, playing: bool = True) -> tuple[bool, str | None]:
        requested_url = source_url.strip()
        if not requested_url:
            await self.clear_track()
            return False, "empty track url"

        resolved_url = await self._resolver.resolve(requested_url)
        if not resolved_url:
            self._last_error = "failed to resolve track url"
            return False, self._last_error

        async with self._playback_lock:
            self._requested_url = requested_url
            self._stream_url = resolved_url
            self._position_seconds = max(0.0, float(position_seconds))
            self._playing = bool(playing)
            self._last_error = ""
            if self._pcm_source:
                await self._pcm_source.set_stream(
                    source_url=self._stream_url,
                    position_seconds=self._position_seconds,
                    playing=self._playing,
                )
        return True, None

    async def clear_track(self) -> None:
        async with self._playback_lock:
            self._requested_url = ""
            self._stream_url = ""
            self._position_seconds = 0.0
            self._playing = False
            self._last_error = ""
            if self._pcm_source:
                await self._pcm_source.clear()

    async def pause(self, position_seconds: float) -> None:
        async with self._playback_lock:
            self._position_seconds = max(0.0, float(position_seconds))
            self._playing = False
            if self._pcm_source:
                await self._pcm_source.set_stream(
                    source_url=self._stream_url,
                    position_seconds=self._position_seconds,
                    playing=False,
                )

    async def resume(self, position_seconds: float) -> None:
        async with self._playback_lock:
            self._position_seconds = max(0.0, float(position_seconds))
            self._playing = True
            if self._pcm_source:
                await self._pcm_source.set_stream(
                    source_url=self._stream_url,
                    position_seconds=self._position_seconds,
                    playing=True,
                )

    async def seek(self, position_seconds: float, playing: bool) -> None:
        async with self._playback_lock:
            self._position_seconds = max(0.0, float(position_seconds))
            self._playing = bool(playing)
            if self._pcm_source:
                await self._pcm_source.set_stream(
                    source_url=self._stream_url,
                    position_seconds=self._position_seconds,
                    playing=self._playing,
                )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": "aiortc-ffmpeg-ytdlp-poc",
            "source_backend": "ffmpeg",
            "source_mode": self._pcm_source.current_mode if self._pcm_source else "unavailable",
            "peer_connections": len(self._peer_connections),
            "playing": self._playing,
            "requested_url": self._requested_url,
            "stream_ready": bool(self._stream_url),
            "yt_dlp_available": self._resolver.available,
            "ffmpeg_available": bool(self._pcm_source and self._pcm_source.available),
            "last_error": self._last_error or None,
            "import_error": AIORTC_IMPORT_ERROR or None,
        }

    async def offer(self, request: web.Request) -> web.Response:
        if not self.enabled:
            return web.json_response(
                {
                    "error": "aiortc or ffmpeg is not available",
                    "details": AIORTC_IMPORT_ERROR or None,
                    "status": self.status(),
                },
                status=503,
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)

        offer_sdp = str(payload.get("sdp", "")).strip()
        offer_type = str(payload.get("type", "")).strip()
        if not offer_sdp or offer_type != "offer":
            return web.json_response({"error": "offer payload required"}, status=400)

        pc = RTCPeerConnection()
        pc_id = f"music-poc-{uuid4().hex[:8]}"
        self._peer_connections.add(pc)
        LOG.info("Created WebRTC music PoC peer: %s", pc_id)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            LOG.info("Peer %s connection state=%s", pc_id, pc.connectionState)
            if pc.connectionState in {"failed", "closed", "disconnected"}:
                await self._close_peer(pc)

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange() -> None:
            LOG.info("Peer %s ICE state=%s", pc_id, pc.iceConnectionState)
            if pc.iceConnectionState in {"failed", "closed"}:
                await self._close_peer(pc)

        await pc.setRemoteDescription(
            RTCSessionDescription(
                sdp=offer_sdp,
                type=offer_type,
            )
        )

        if self._relay and self._source_track:
            pc.addTrack(self._relay.subscribe(self._source_track))

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return web.json_response(
            {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "status": self.status(),
            }
        )

    async def debug_page(self, _: web.Request) -> web.Response:
        return web.Response(text=_build_debug_html(self.status()), content_type="text/html")

    async def status_endpoint(self, _: web.Request) -> web.Response:
        return web.json_response(self.status())

    async def shutdown(self) -> None:
        peers = list(self._peer_connections)
        for pc in peers:
            await self._close_peer(pc)
        if self._pcm_source:
            await self._pcm_source.shutdown()

    async def _close_peer(self, peer_connection: Any) -> None:
        if peer_connection not in self._peer_connections:
            return
        self._peer_connections.discard(peer_connection)
        try:
            await peer_connection.close()
        except Exception:  # noqa: BLE001
            LOG.debug("Ignoring peer close failure", exc_info=True)


def _build_debug_html(status: dict[str, Any]) -> str:
    status_json = json.dumps(status, ensure_ascii=True, indent=2)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scratch House WebRTC Music PoC</title>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #111827;
      --line: #334155;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --accent: #f59e0b;
      --accent-2: #22c55e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "SF Mono", "Menlo", monospace;
      background:
        radial-gradient(circle at top, rgba(245, 158, 11, 0.16), transparent 30%),
        linear-gradient(180deg, #020617 0%, var(--bg) 100%);
      color: var(--text);
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    main {{
      width: min(880px, 100%);
      background: rgba(17, 24, 39, 0.88);
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 18px;
      padding: 24px;
      backdrop-filter: blur(14px);
      box-shadow: 0 20px 80px rgba(0, 0, 0, 0.35);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
    }}
    p {{
      color: var(--muted);
      line-height: 1.5;
    }}
    .row {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 20px 0;
    }}
    button {{
      appearance: none;
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      background: var(--accent);
      color: #111827;
      font: inherit;
      cursor: pointer;
      font-weight: 700;
    }}
    button.secondary {{
      background: #1f2937;
      color: var(--text);
      border: 1px solid var(--line);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(34, 197, 94, 0.12);
      color: #bbf7d0;
      border: 1px solid rgba(34, 197, 94, 0.2);
    }}
    pre {{
      overflow: auto;
      padding: 16px;
      border-radius: 14px;
      background: rgba(2, 6, 23, 0.9);
      border: 1px solid rgba(51, 65, 85, 0.7);
      color: #cbd5e1;
      line-height: 1.4;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Scratch House WebRTC Music PoC</h1>
    <p>
      This page verifies that the server can publish a WebRTC audio track like a virtual DJ.
      The server resolves queued tracks with yt-dlp, decodes them through FFmpeg, and returns the audio over WebRTC.
    </p>
    <div class="row">
      <button id="start">Start Receiver</button>
      <button id="stop" class="secondary">Stop</button>
      <span class="badge" id="state">idle</span>
    </div>
    <audio id="remoteAudio" autoplay controls></audio>
    <h2>Server Status</h2>
    <pre id="status">{status_json}</pre>
  </main>
  <script>
    let pc = null;
    let remoteStream = null;

    async function waitForIceGatheringComplete(peer) {{
      if (peer.iceGatheringState === "complete") {{
        return;
      }}
      await new Promise((resolve) => {{
        function checkState() {{
          if (peer.iceGatheringState === "complete") {{
            peer.removeEventListener("icegatheringstatechange", checkState);
            resolve();
          }}
        }}
        peer.addEventListener("icegatheringstatechange", checkState);
      }});
    }}

    async function refreshStatus() {{
      const response = await fetch("/api/webrtc/status");
      const payload = await response.json();
      document.getElementById("status").textContent = JSON.stringify(payload, null, 2);
    }}

    async function start() {{
      if (pc) {{
        return;
      }}

      document.getElementById("state").textContent = "connecting";
      pc = new RTCPeerConnection();
      remoteStream = new MediaStream();
      document.getElementById("remoteAudio").srcObject = remoteStream;

      pc.addTransceiver("audio", {{ direction: "recvonly" }});
      pc.addEventListener("track", (event) => {{
        remoteStream.addTrack(event.track);
      }});
      pc.addEventListener("connectionstatechange", () => {{
        document.getElementById("state").textContent = pc.connectionState;
      }});

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitForIceGatheringComplete(pc);

      const response = await fetch("/api/webrtc/music-offer", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(pc.localDescription),
      }});
      const answer = await response.json();
      if (!response.ok) {{
        document.getElementById("state").textContent = "error";
        document.getElementById("status").textContent = JSON.stringify(answer, null, 2);
        await stop();
        return;
      }}

      await pc.setRemoteDescription(answer);
      await document.getElementById("remoteAudio").play();
      await refreshStatus();
    }}

    async function stop() {{
      if (pc) {{
        await pc.close();
      }}
      pc = null;
      remoteStream = null;
      document.getElementById("remoteAudio").srcObject = null;
      document.getElementById("state").textContent = "idle";
      await refreshStatus();
    }}

    document.getElementById("start").addEventListener("click", () => {{
      start().catch(async (error) => {{
        document.getElementById("state").textContent = "error";
        document.getElementById("status").textContent = String(error);
        await stop();
      }});
    }});
    document.getElementById("stop").addEventListener("click", () => {{
      stop().catch(() => undefined);
    }});
    refreshStatus().catch(() => undefined);
  </script>
</body>
</html>
"""
