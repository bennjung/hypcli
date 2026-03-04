from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import platform
import shutil
import shlex
import signal
import subprocess
import tempfile
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

from websockets.client import connect
from websockets.exceptions import ConnectionClosed

LOG = logging.getLogger("scratch_house.client")


class LocalView:
    def __init__(self) -> None:
        self.users: list[dict[str, Any]] = []
        self.ranking: list[dict[str, Any]] = []
        self.board: dict[str, Any] = {}
        self.music: dict[str, Any] = {}
        self.queue: list[dict[str, Any]] = []
        self.host: str | None = None


class ClaudeUsageTracker:
    def __init__(self, project_path: str) -> None:
        self.project_path = os.path.abspath(project_path)
        self.project_key = self._to_project_key(self.project_path)
        self.project_dir = os.path.join(os.path.expanduser("~/.claude/projects"), self.project_key)

    def get_token_usage(self) -> int | None:
        if not os.path.isdir(self.project_dir):
            return None

        session_files = glob.glob(os.path.join(self.project_dir, "*.jsonl"))
        if not session_files:
            return None

        latest = max(session_files, key=os.path.getmtime)
        total_tokens = 0
        try:
            with open(latest, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = payload.get("message")
                    if not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    total_tokens += int(usage.get("input_tokens", 0) or 0)
                    total_tokens += int(usage.get("output_tokens", 0) or 0)
                    total_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
                    total_tokens += int(usage.get("cache_read_input_tokens", 0) or 0)
        except OSError:
            return None

        return max(0, total_tokens)

    @staticmethod
    def _to_project_key(path: str) -> str:
        normalized = os.path.abspath(path).replace("\\", "/").strip("/")
        return "-" + normalized.replace("/", "-")


class YtDlpResolver:
    def __init__(
        self,
        enabled: bool = True,
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.on_notice = on_notice
        self.binary = shutil.which("yt-dlp") if enabled else None
        self._missing_notified = False
        self._cache: dict[str, tuple[str, float]] = {}
        self.cache_ttl_seconds = 600

    async def resolve(self, source_url: str) -> str:
        source = source_url.strip()
        if not source:
            return source_url
        if not self.enabled:
            return source
        if not self.binary:
            if not self._missing_notified:
                self._missing_notified = True
                self._notice("yt-dlp not found. fallback to original URL playback.")
            return source
        if not self._looks_like_url(source):
            return source

        cached = self._cache.get(source)
        if cached and cached[1] > time.time():
            return cached[0]

        resolved = await asyncio.to_thread(self._resolve_blocking, source)
        if not resolved:
            return source

        if resolved != source:
            self._cache[source] = (resolved, time.time() + self.cache_ttl_seconds)
        return resolved

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
            self._notice("yt-dlp execution failed. fallback to original URL.")
            return None

        if completed.returncode != 0:
            self._notice("yt-dlp could not resolve stream URL. fallback applied.")
            return None

        for line in completed.stdout.splitlines():
            value = line.strip()
            if value:
                return value
        return None

    def _notice(self, message: str) -> None:
        if self.on_notice:
            self.on_notice(message)

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        lowered = value.lower()
        return lowered.startswith("http://") or lowered.startswith("https://")


class MpvPlayer:
    def __init__(
        self,
        enabled: bool,
        on_notice: Callable[[str], None] | None = None,
        enable_yt_dlp: bool = True,
    ) -> None:
        self.enabled = enabled
        self.process: subprocess.Popen[Any] | None = None
        self.current_url = ""
        self.current_stream_url = ""
        self.ipc_path: str | None = None
        self.paused = False
        self.reference_position = 0.0
        self.reference_playing = False
        self.reference_monotonic = time.monotonic()
        self.resolver = YtDlpResolver(enabled=enable_yt_dlp, on_notice=on_notice)

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None
        self.current_url = ""
        self.current_stream_url = ""
        self._cleanup_ipc_path()
        self.paused = False
        self.reference_position = 0.0
        self.reference_playing = False
        self.reference_monotonic = time.monotonic()

    def _cleanup_ipc_path(self) -> None:
        if self.ipc_path and os.path.exists(self.ipc_path):
            try:
                os.unlink(self.ipc_path)
            except OSError:
                pass
        self.ipc_path = None

    def _is_alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def _supports_ipc(self) -> bool:
        return os.name != "nt"

    async def play(self, url: str, position_seconds: float) -> None:
        if not self.enabled:
            return

        self.close()
        position = max(0.0, position_seconds)
        stream_url = await self.resolver.resolve(url)
        args = ["mpv", "--no-video", "--quiet", f"--start={position:.2f}"]

        if self._supports_ipc():
            socket_name = f"scratch-house-mpv-{os.getpid()}-{int(time.time() * 1000)}.sock"
            self.ipc_path = os.path.join(tempfile.gettempdir(), socket_name)
            args.append(f"--input-ipc-server={self.ipc_path}")

        args.append(stream_url)

        try:
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.enabled = False
            self._cleanup_ipc_path()
            return

        self.current_url = url
        self.current_stream_url = stream_url
        self.paused = False
        self.update_reference(position, True)

        if self.ipc_path:
            await self._wait_for_ipc()

    async def pause(self) -> None:
        if not self.enabled or not self._is_alive():
            return

        current = self.estimated_position()
        if await self._send_command(["set_property", "pause", True]):
            self.paused = True
            self.update_reference(current, False)
            return

        if hasattr(signal, "SIGSTOP"):
            self.process.send_signal(signal.SIGSTOP)
            self.paused = True
            self.update_reference(current, False)

    async def resume(self) -> None:
        if not self.enabled or not self._is_alive():
            return

        current = self.estimated_position()
        if await self._send_command(["set_property", "pause", False]):
            self.paused = False
            self.update_reference(current, True)
            return

        if hasattr(signal, "SIGCONT"):
            self.process.send_signal(signal.SIGCONT)
            self.paused = False
            self.update_reference(current, True)

    async def seek(self, position_seconds: float) -> None:
        if not self.enabled:
            return

        position = max(0.0, position_seconds)
        playing = self.reference_playing and not self.paused

        if await self._send_command(["set_property", "time-pos", position]):
            self.update_reference(position, playing)
            return

        if self.current_stream_url:
            await self.play(self.current_url or self.current_stream_url, position)
            if not playing:
                await self.pause()

    def update_reference(self, position_seconds: float, playing: bool) -> None:
        self.reference_position = max(0.0, position_seconds)
        self.reference_playing = playing
        self.reference_monotonic = time.monotonic()

    def estimated_position(self) -> float:
        if self.reference_playing:
            return max(0.0, self.reference_position + (time.monotonic() - self.reference_monotonic))
        return self.reference_position

    async def _wait_for_ipc(self, timeout_seconds: float = 1.5) -> bool:
        if not self.ipc_path:
            return False

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if os.path.exists(self.ipc_path):
                return True
            await asyncio.sleep(0.05)

        return False

    async def _send_command(self, command: list[Any]) -> bool:
        if not self.ipc_path or not self._is_alive() or not os.path.exists(self.ipc_path):
            return False

        try:
            reader, writer = await asyncio.open_unix_connection(self.ipc_path)
            payload = json.dumps({"command": command}) + "\n"
            writer.write(payload.encode("utf-8"))
            await writer.drain()
            try:
                await asyncio.wait_for(reader.readline(), timeout=0.5)
            except TimeoutError:
                pass
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False


class ScratchHouseClient:
    def __init__(
        self,
        server_url: str,
        name: str,
        mpv_enabled: bool,
        enable_yt_dlp: bool,
        link_via_telegram: bool,
        device_name: str,
        claude_project_path: str,
    ) -> None:
        self.server_url = server_url
        self.name = name
        self.link_via_telegram = link_via_telegram
        self.device_name = device_name
        self.usage_tracker = ClaudeUsageTracker(project_path=claude_project_path)

        self.view = LocalView()
        self.player = MpvPlayer(
            enabled=mpv_enabled,
            on_notice=self._push_event,
            enable_yt_dlp=enable_yt_dlp,
        )
        self._running = True
        self._linked = not link_via_telegram
        self._events: deque[str] = deque(maxlen=8)
        self._display_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._last_reported_token_usage: int | None = None

    async def run(self) -> None:
        try:
            async with connect(self.server_url) as websocket:
                await self._send_json(
                    websocket,
                    {
                        "type": "join",
                        "name": self.name,
                        "link_via_telegram": self.link_via_telegram,
                        "device_name": self.device_name,
                    },
                )
                self._push_event(f"Connected to {self.server_url}")
                self._push_event("Type command and press Enter. Use /help.")
                await self.render_screen()

                receiver = asyncio.create_task(self._receive_loop(websocket))
                sender = asyncio.create_task(self._input_loop(websocket))
                usage_reporter = asyncio.create_task(self._token_usage_loop(websocket))
                done, pending = await asyncio.wait(
                    [receiver, sender, usage_reporter],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    exc = task.exception()
                    if exc:
                        raise exc
        finally:
            self.player.close()

    async def _receive_loop(self, websocket: Any) -> None:
        async for raw in websocket:
            payload = self._parse(raw)
            await self._handle_server_message(payload)

    async def _token_usage_loop(self, websocket: Any) -> None:
        while self._running:
            await asyncio.sleep(8)
            if not self._linked:
                continue
            token_usage = await asyncio.to_thread(self.usage_tracker.get_token_usage)
            if token_usage is None:
                continue
            if token_usage == self._last_reported_token_usage:
                continue
            self._last_reported_token_usage = token_usage
            try:
                await self._send_json(
                    websocket,
                    {"type": "token_usage_report", "token_usage": token_usage},
                )
            except Exception:
                return

    async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await websocket.send(json.dumps(payload))

    async def _input_loop(self, websocket: Any) -> None:
        while self._running:
            line = await asyncio.to_thread(input)
            line = line.strip()
            if not line:
                continue
            await self._handle_user_input(websocket, line)

    async def _handle_user_input(self, websocket: Any, line: str) -> None:
        normalized = line.strip().lower()

        if normalized in {"q", "quit"}:
            await self._execute_command(websocket, "/quit")
            return
        if normalized == "w":
            await self._execute_command(websocket, "/work")
            return
        if normalized == "r":
            await self._execute_command(websocket, "/rest")
            return
        if normalized == "m":
            await self._execute_command(websocket, "/mute toggle")
            return
        if normalized == "n":
            await self._execute_command(websocket, "/next")
            return
        if normalized == "x":
            await self._execute_command(websocket, "/close")
            return

        if not line.startswith("/"):
            self._push_event("Slash command only. Use /help.")
            await self.render_screen()
            return

        await self._execute_command(websocket, line)

    async def _execute_command(self, websocket: Any, line: str) -> None:
        try:
            command, *args = shlex.split(line)
        except ValueError:
            self._push_event("Parse error: invalid quote")
            await self.render_screen()
            return

        cmd = command.lower()

        if cmd == "/help":
            self._push_event("/play /skip /next /close /pause /resume /seek /queue /users /ranking /quit")
            await self.render_screen()
            return

        if cmd == "/quit":
            self._running = False
            await websocket.close()
            return

        if not self._linked:
            self._push_event("Waiting for Telegram /link approval")
            await self.render_screen()
            return

        if cmd == "/work":
            await self._send_json(websocket, {"type": "set_mute", "muted": False})
            await self._send_json(websocket, {"type": "set_speaking", "speaking": True})
            self._push_event("Status set to work")
            await self.render_screen()
            return

        if cmd == "/rest":
            await self._send_json(websocket, {"type": "set_speaking", "speaking": False})
            self._push_event("Status set to rest")
            await self.render_screen()
            return

        if cmd == "/play" and args:
            await self._send_json(websocket, {"type": "play", "url": args[0]})
            self._push_event("Enqueued track")
            await self.render_screen()
            return

        if cmd == "/skip":
            await self._send_json(websocket, {"type": "skip"})
            self._push_event("Skip request sent")
            await self.render_screen()
            return

        if cmd == "/next":
            await self._send_json(websocket, {"type": "next"})
            await self.render_screen()
            return

        if cmd == "/close":
            await self._send_json(websocket, {"type": "close"})
            self._push_event("Close requested (host only)")
            await self.render_screen()
            return

        if cmd == "/pause":
            await self._send_json(websocket, {"type": "pause"})
            await self.render_screen()
            return

        if cmd == "/resume":
            await self._send_json(websocket, {"type": "resume"})
            await self.render_screen()
            return

        if cmd == "/seek" and args:
            try:
                position_seconds = float(args[0])
            except ValueError:
                self._push_event("/seek requires a numeric value")
                await self.render_screen()
                return
            await self._send_json(websocket, {"type": "seek", "position_seconds": position_seconds})
            await self.render_screen()
            return

        if cmd == "/board" and args:
            await self._send_json(websocket, {"type": "board_update", "content": " ".join(args)})
            await self.render_screen()
            return

        if cmd == "/mute":
            state: bool
            if args and args[0].lower() in {"on", "true", "1"}:
                state = True
            elif args and args[0].lower() in {"off", "false", "0"}:
                state = False
            else:
                me = self._find_user(self.name)
                state = not bool(me.get("muted", False)) if me else True
            await self._send_json(websocket, {"type": "set_mute", "muted": state})
            await self.render_screen()
            return

        if cmd == "/speak" and args:
            state = args[0].lower() in {"on", "true", "1"}
            await self._send_json(websocket, {"type": "set_speaking", "speaking": state})
            await self.render_screen()
            return

        if cmd == "/users":
            await self.render_screen()
            return

        if cmd == "/ranking":
            await self.render_screen()
            return

        if cmd == "/queue":
            await self.render_screen()
            return

        self._push_event("Unknown command or missing args. Use /help")
        await self.render_screen()

    async def _handle_server_message(self, payload: dict[str, Any]) -> None:
        msg_type = payload.get("type")

        if msg_type == "link_required":
            session_id = payload.get("session_id")
            device_name = payload.get("device_name")
            self._push_event(f"Telegram link required: session={session_id} device={device_name}")
            await self.render_screen()
            return

        if msg_type == "link_success":
            self.name = str(payload.get("name", self.name))
            self._push_event(f"Linked as {self.name}")
            await self.render_screen()
            return

        if msg_type == "sync_init":
            self.view.users = payload.get("users", [])
            self.view.ranking = payload.get("ranking", [])
            self.view.board = payload.get("board", {})
            self.view.music = payload.get("music", {})
            self.view.queue = payload.get("queue", [])
            self.view.host = payload.get("host")
            self._linked = True
            self._push_event("State synced")
            await self._sync_mpv()
            await self.render_screen()
            return

        if msg_type == "user_state":
            self.view.users = payload.get("users", [])
            await self.render_screen()
            return

        if msg_type == "ranking_state":
            self.view.ranking = payload.get("ranking", [])
            await self.render_screen()
            return

        if msg_type == "board_state":
            self.view.board = payload.get("board", {})
            self._push_event(f"Board updated by {self.view.board.get('updated_by')}")
            await self.render_screen()
            return

        if msg_type == "music_state":
            self.view.music = payload.get("music", {})
            await self._sync_mpv()
            await self.render_screen()
            return

        if msg_type == "queue_state":
            self.view.queue = payload.get("queue", [])
            await self.render_screen()
            return

        if msg_type == "host_state":
            self.view.host = payload.get("host")
            await self.render_screen()
            return

        if msg_type == "skip_request":
            self._push_event(
                f"Skip requested by {payload.get('from')} for {payload.get('current_url')}"
            )
            await self.render_screen()
            return

        if msg_type == "system":
            self._push_event(str(payload.get("message", "")))
            await self.render_screen()
            return

        if msg_type == "error":
            self._push_event(f"Server error: {payload.get('error')}")
            await self.render_screen()

    async def _sync_mpv(self) -> None:
        music = self.view.music
        url = str(music.get("url", "")).strip()
        if not url:
            self.player.close()
            return

        playing = bool(music.get("playing", False))
        position = float(music.get("position_seconds", 0.0))

        if self.player.current_url != url:
            await self.player.play(url, position)
            if not playing:
                await self.player.pause()
            self.player.update_reference(position, playing)
            return

        drift = abs(position - self.player.estimated_position())
        if drift >= 0.75:
            await self.player.seek(position)

        if playing and self.player.paused:
            await self.player.resume()
        if not playing and not self.player.paused:
            await self.player.pause()

        self.player.update_reference(position, playing)

    async def render_screen(self) -> None:
        ui = self._build_tui()
        async with self._display_lock:
            print("\033[2J\033[H" + ui, end="", flush=True)

    def _build_tui(self) -> str:
        host_name = (self.view.host or "-")[:16]
        start_time = self._room_start_time()
        member_count = len(self.view.users)

        song_url = str(self.view.music.get("url", ""))
        if not song_url:
            song_url = "-"
        song_short = song_url if len(song_url) <= 70 else song_url[:67] + "..."
        play_pos = self._format_mmss(float(self.view.music.get("position_seconds", 0.0)))
        play_state = "LIVE" if bool(self.view.music.get("playing", False)) else "PAUSE"

        lines: list[str] = []
        lines.append("================ Focus Room ================")
        lines.append("--- Host ---      --- Focus Info -----------")
        lines.append(f"| [{host_name:<16}] |  | Start   : {start_time:<8} |")
        lines.append(f"| {'':16} |  | Members : {member_count:<8} |")
        lines.append("------------------  ------------------------")
        lines.append("")

        lines.append("-- Playing ---------------------------------")
        lines.append(f"| Song    : {song_short:<70}|")
        lines.append(f"| Playing : {play_pos:<8} ({play_state}){'':<53}|")
        lines.append("--------------------------------------------")
        lines.append("")

        lines.append("-- Members ---------------------------------")
        if not self.view.users:
            lines.append("| (none)                                   |")
        else:
            for user in self.view.users[:10]:
                name = str(user.get("name", "-"))[:18]
                muted = "M" if user.get("muted") else "-"
                speaking = "S" if user.get("speaking") else "-"
                role = "HOST" if name == (self.view.host or "") else "MEM"
                lines.append(f"| {name:<18} [{muted}{speaking}] {role:<4}                     |")
        lines.append("--------------------------------------------")
        lines.append("")

        lines.append("-- LeaderBoard --------------------------------------------------------")
        lines.append("| Name         Start   Plan   Status   Token Usage                    |")
        lines.append("| ----------   -----   ----   ------   -----------                    |")
        for row in self._leaderboard_rows()[:8]:
            lines.append(
                f"| {row['name']:<12} {row['start']:<7} {row['plan']:<6} {row['status']:<8} {row['token']:<11}        |"
            )
        if not self._leaderboard_rows():
            lines.append("| (empty)                                                            |")
        lines.append("------------------------------------------------------------------------")
        lines.append("")

        board_content = str(self.view.board.get("content", "")).strip()
        board_short = board_content if len(board_content) <= 72 else board_content[:69] + "..."
        lines.append(f"Board: {board_short or '-'}")
        lines.append("")

        lines.append("Events:")
        if not self._events:
            lines.append("- (none)")
        else:
            for event in self._events:
                lines.append(f"- {event}")

        lines.append("")
        lines.append("[Q] Quit  [W] Work  [R] Rest  [M] Mute  [N] Next  [X] Close Session")
        lines.append("Type command and press Enter (ex: /play <url>, /skip, /queue, /close)")
        lines.append("")

        return "\n".join(lines)

    def _leaderboard_rows(self) -> list[dict[str, str]]:
        users_by_name: dict[str, dict[str, Any]] = {}
        for user in self.view.users:
            name = str(user.get("name", ""))
            if name and name not in users_by_name:
                users_by_name[name] = user

        plan_counts: dict[str, int] = {}
        for item in self.view.queue:
            requested_by = str(item.get("requested_by", ""))
            if requested_by:
                plan_counts[requested_by] = plan_counts.get(requested_by, 0) + 1

        current_requested_by = str(self.view.music.get("requested_by", ""))
        if current_requested_by:
            plan_counts[current_requested_by] = plan_counts.get(current_requested_by, 0) + 1

        rows: list[dict[str, str]] = []
        for rank_item in self.view.ranking:
            name = str(rank_item.get("name", "-"))
            user = users_by_name.get(name)
            joined = self._format_hhmm((user or {}).get("joined_at"))
            status = "idle"
            if user:
                if user.get("speaking"):
                    status = "work"
                elif user.get("muted"):
                    status = "mute"
                else:
                    status = "rest"

            token_raw = int(rank_item.get("token_usage", 0) or 0)
            rows.append(
                {
                    "name": name[:12],
                    "start": joined,
                    "plan": str(plan_counts.get(name, 0)),
                    "status": status,
                    "token": self._format_token_usage(token_raw),
                }
            )
        return rows

    def _push_event(self, message: str) -> None:
        if not message:
            return
        self._events.appendleft(message)

    def _find_user(self, name: str) -> dict[str, Any] | None:
        for user in self.view.users:
            if str(user.get("name", "")) == name:
                return user
        return None

    def _room_start_time(self) -> str:
        if not self.view.users:
            return "--:--"
        joined_times: list[datetime] = []
        for user in self.view.users:
            raw = user.get("joined_at")
            if isinstance(raw, str):
                try:
                    joined_times.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
                except ValueError:
                    continue
        if not joined_times:
            return "--:--"
        return min(joined_times).strftime("%H:%M")

    @staticmethod
    def _format_hhmm(raw: Any) -> str:
        if not isinstance(raw, str):
            return "--:--"
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return "--:--"
        return dt.strftime("%H:%M")

    @staticmethod
    def _format_mmss(seconds: float) -> str:
        total = max(0, int(seconds))
        return f"{total // 60:02d}:{total % 60:02d}"

    @staticmethod
    def _format_token_usage(tokens: int) -> str:
        if tokens <= 0:
            return "0"
        if tokens < 1000:
            return str(tokens)
        return f"{tokens / 1000:.1f}k"

    @staticmethod
    def _parse(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scratch House terminal client")
    parser.add_argument("--server", default="ws://127.0.0.1:8765", help="WebSocket server URL")
    parser.add_argument("--name", default="", help="Display name (manual mode)")
    parser.add_argument(
        "--link-telegram",
        action="store_true",
        help="Require Telegram /link approval before joining",
    )
    parser.add_argument(
        "--device-name",
        default=platform.node() or "local-device",
        help="Device label shown in Telegram session list",
    )
    parser.add_argument(
        "--claude-project-path",
        default=os.getcwd(),
        help="Project path used to read Claude Code token usage logs",
    )
    parser.add_argument("--mpv", action="store_true", help="Enable local mpv playback")
    parser.add_argument(
        "--disable-yt-dlp",
        action="store_true",
        help="Disable yt-dlp stream extraction and play original URLs directly",
    )
    parser.add_argument("--log-level", default="WARNING", help="Logging level")
    args = parser.parse_args()

    if not args.link_telegram and not args.name:
        parser.error("--name is required unless --link-telegram is used")

    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    client = ScratchHouseClient(
        server_url=args.server,
        name=args.name or "link-pending",
        mpv_enabled=args.mpv,
        enable_yt_dlp=not bool(args.disable_yt_dlp),
        link_via_telegram=bool(args.link_telegram),
        device_name=str(args.device_name),
        claude_project_path=str(args.claude_project_path),
    )
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass
    except ConnectionClosed as exc:
        LOG.error("Disconnected: %s", exc)


if __name__ == "__main__":
    main()
