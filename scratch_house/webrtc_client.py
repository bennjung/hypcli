from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from typing import Any, Callable

import aiohttp

LOG = logging.getLogger("scratch_house.webrtc_client")

try:
    from av.audio.resampler import AudioResampler
    from aiortc import RTCPeerConnection, RTCSessionDescription

    AIORTC_CLIENT_AVAILABLE = True
    AIORTC_CLIENT_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - optional runtime dependency
    AudioResampler = None  # type: ignore[assignment]
    RTCPeerConnection = None  # type: ignore[assignment]
    RTCSessionDescription = None  # type: ignore[assignment]
    AIORTC_CLIENT_AVAILABLE = False
    AIORTC_CLIENT_IMPORT_ERROR = str(exc)


class WebrtcMusicReceiver:
    def __init__(
        self,
        enabled: bool,
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.on_notice = on_notice
        self.ffplay_binary = shutil.which("ffplay")
        self.pc: Any | None = None
        self.session: aiohttp.ClientSession | None = None
        self.ffplay_process: subprocess.Popen[bytes] | None = None
        self.consumer_task: asyncio.Task[None] | None = None
        self.writer_task: asyncio.Task[None] | None = None
        self.pcm_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.api_base = ""
        self.connected = False
        self._notice_cache: set[str] = set()

    @property
    def available(self) -> bool:
        return self.enabled and AIORTC_CLIENT_AVAILABLE and bool(self.ffplay_binary)

    @property
    def status(self) -> str:
        if self.connected:
            return "connected"
        if not self.enabled:
            return "disabled"
        if not AIORTC_CLIENT_AVAILABLE:
            return "aiortc-missing"
        if not self.ffplay_binary:
            return "ffplay-missing"
        return "idle"

    async def ensure_started(self, api_base: str) -> bool:
        normalized = api_base.rstrip("/")
        if not normalized:
            self._notice_once("WebRTC API base not configured. keeping legacy playback path.")
            return False
        if self.connected and self.api_base == normalized and self.pc:
            return True
        return await self.start(normalized)

    async def start(self, api_base: str) -> bool:
        await self.stop()
        if not self.available:
            if not AIORTC_CLIENT_AVAILABLE:
                self._notice_once(f"aiortc not available for WebRTC receive: {AIORTC_CLIENT_IMPORT_ERROR}")
            elif not self.ffplay_binary:
                self._notice_once("ffplay not found. cannot output WebRTC audio locally.")
            return False

        self.api_base = api_base.rstrip("/")
        self.session = aiohttp.ClientSession()
        self.pc = RTCPeerConnection()
        self.connected = False
        self.writer_task = asyncio.create_task(self._writer_loop())

        @self.pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind != "audio":
                return
            if self.consumer_task:
                self.consumer_task.cancel()
            self.consumer_task = asyncio.create_task(self._consume_audio(track))

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = self.pc.connectionState if self.pc else "closed"
            if state == "connected":
                self.connected = True
                self._notice("WebRTC music receiver connected")
                return
            if state in {"failed", "closed", "disconnected"}:
                self.connected = False
                if state != "closed":
                    self._notice(f"WebRTC music receiver disconnected: {state}")

        self.pc.addTransceiver("audio", direction="recvonly")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        await self._wait_for_ice_gathering_complete(self.pc)

        assert self.session is not None
        async with self.session.post(
            f"{self.api_base}/api/webrtc/music-offer",
            json={
                "sdp": self.pc.localDescription.sdp,
                "type": self.pc.localDescription.type,
            },
        ) as response:
            payload = await response.json()
            if response.status >= 400:
                self._notice(f"WebRTC offer failed: {payload.get('error', response.status)}")
                await self.stop()
                return False
            await self.pc.setRemoteDescription(
                RTCSessionDescription(
                    sdp=payload["sdp"],
                    type=payload["type"],
                )
            )

        return True

    async def stop(self) -> None:
        self.connected = False
        if self.consumer_task:
            self.consumer_task.cancel()
            try:
                await self.consumer_task
            except asyncio.CancelledError:
                pass
            self.consumer_task = None

        if self.writer_task:
            self.writer_task.cancel()
            try:
                await self.writer_task
            except asyncio.CancelledError:
                pass
            self.writer_task = None

        if self.pc:
            try:
                await self.pc.close()
            except Exception:  # noqa: BLE001
                LOG.debug("Ignoring RTCPeerConnection close failure", exc_info=True)
            self.pc = None

        if self.session:
            await self.session.close()
            self.session = None

        self._stop_ffplay()
        self.api_base = ""
        self._drain_queue()

    async def _consume_audio(self, track: Any) -> None:
        assert AudioResampler is not None
        resampler = AudioResampler(format="s16", layout="mono", rate=48_000)
        try:
            while True:
                frame = await track.recv()
                resampled = resampler.resample(frame)
                frames = resampled if isinstance(resampled, list) else [resampled]
                for audio_frame in frames:
                    if audio_frame is None:
                        continue
                    payload = bytes(audio_frame.planes[0])
                    if not payload:
                        continue
                    if self.pcm_queue.full():
                        try:
                            self.pcm_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    await self.pcm_queue.put(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOG.exception("WebRTC audio consumer failed")
            self._notice("WebRTC audio consumer stopped unexpectedly")

    async def _writer_loop(self) -> None:
        try:
            while True:
                payload = await self.pcm_queue.get()
                if not payload:
                    continue
                self._ensure_ffplay()
                process = self.ffplay_process
                if not process or not process.stdin:
                    continue
                try:
                    await asyncio.to_thread(process.stdin.write, payload)
                    await asyncio.to_thread(process.stdin.flush)
                except BrokenPipeError:
                    self._stop_ffplay()
                    continue
                except Exception:  # noqa: BLE001
                    LOG.exception("ffplay writer failed")
                    self._stop_ffplay()
        except asyncio.CancelledError:
            raise

    def _ensure_ffplay(self) -> None:
        if self.ffplay_process and self.ffplay_process.poll() is None:
            return
        if not self.ffplay_binary:
            return
        self.ffplay_process = subprocess.Popen(
            [
                self.ffplay_binary,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_ffplay(self) -> None:
        process = self.ffplay_process
        self.ffplay_process = None
        if not process:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            except Exception:  # noqa: BLE001
                LOG.debug("Ignoring ffplay stdin close failure", exc_info=True)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

    def _drain_queue(self) -> None:
        while not self.pcm_queue.empty():
            try:
                self.pcm_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _notice_once(self, message: str) -> None:
        if message in self._notice_cache:
            return
        self._notice_cache.add(message)
        self._notice(message)

    def _notice(self, message: str) -> None:
        if self.on_notice:
            self.on_notice(message)

    @staticmethod
    async def _wait_for_ice_gathering_complete(peer: Any) -> None:
        if peer.iceGatheringState == "complete":
            return
        done = asyncio.get_running_loop().create_future()

        @peer.on("icegatheringstatechange")
        def on_state() -> None:
            if peer.iceGatheringState == "complete" and not done.done():
                done.set_result(None)

        await asyncio.wait_for(done, timeout=5)
