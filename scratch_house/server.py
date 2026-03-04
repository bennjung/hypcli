from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiohttp import web
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServerProtocol, serve

from scratch_house.models import BoardState, MusicState, QueueItem, UserSession, utc_now

LOG = logging.getLogger("scratch_house.server")


@dataclass
class PendingLinkSession:
    session_id: str
    websocket: WebSocketServerProtocol
    device_name: str
    created_at: datetime = field(default_factory=utc_now)

    def snapshot(self) -> dict[str, Any]:
        age_seconds = int((utc_now() - self.created_at).total_seconds())
        return {
            "session_id": self.session_id,
            "device_name": self.device_name,
            "created_at": self.created_at.isoformat(),
            "age_seconds": max(0, age_seconds),
        }


@dataclass
class LoungeState:
    users: dict[WebSocketServerProtocol, UserSession] = field(default_factory=dict)
    accumulated_active_seconds: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    token_usage_by_user: dict[str, int] = field(default_factory=dict)
    music: MusicState = field(default_factory=MusicState)
    music_queue: deque[QueueItem] = field(default_factory=deque)
    next_queue_id: int = 1
    host_connection: WebSocketServerProtocol | None = None
    board: BoardState = field(default_factory=BoardState)

    def leaderboard(self) -> list[dict[str, Any]]:
        totals: dict[str, int] = dict(self.accumulated_active_seconds)
        for _, session in self.users.items():
            totals[session.name] = totals.get(session.name, 0) + session.active_seconds()

        ranked = list(totals.items())
        ranked.sort(key=lambda item: item[1], reverse=True)
        return [
            {
                "rank": i + 1,
                "name": name,
                "active_seconds": seconds,
                "token_usage": int(self.token_usage_by_user.get(name, 0)),
            }
            for i, (name, seconds) in enumerate(ranked)
        ]

    def user_snapshots(self) -> list[dict[str, Any]]:
        return [session.snapshot() for session in self.users.values()]

    def queue_snapshots(self) -> list[dict[str, Any]]:
        return [item.snapshot() for item in self.music_queue]

    def enqueue_track(self, url: str, requested_by: str) -> QueueItem:
        item = QueueItem(
            queue_id=self.next_queue_id,
            url=url,
            requested_by=requested_by,
        )
        self.next_queue_id += 1
        self.music_queue.append(item)
        return item

    def pop_next_track(self) -> QueueItem | None:
        if not self.music_queue:
            return None
        return self.music_queue.popleft()

    def host_name(self) -> str | None:
        if self.host_connection and self.host_connection in self.users:
            return self.users[self.host_connection].name
        return None

    def ensure_host_connection(self) -> bool:
        if self.host_connection and self.host_connection in self.users:
            return False
        if not self.users:
            self.host_connection = None
            return False
        self.host_connection = min(
            self.users.items(),
            key=lambda item: item[1].joined_at,
        )[0]
        return True


class ScratchHouseServer:
    def __init__(
        self,
        link_api_token: str = "",
        link_ttl_seconds: int = 120,
        reports_dir: str = "reports",
    ) -> None:
        self.state = LoungeState()
        self.pending_link_sessions: dict[str, PendingLinkSession] = {}
        self.pending_by_ws: dict[WebSocketServerProtocol, str] = {}
        self.telegram_identity_by_ws: dict[WebSocketServerProtocol, str] = {}
        self.link_api_token = link_api_token.strip()
        self.link_ttl_seconds = max(30, int(link_ttl_seconds))
        self.reports_dir = reports_dir
        self._tick_task: asyncio.Task[None] | None = None
        self._http_runner: web.AppRunner | None = None

    async def start(self, host: str, port: int, api_host: str, api_port: int) -> None:
        self._tick_task = asyncio.create_task(self._periodic_ranking_broadcast())
        if api_port > 0:
            await self._start_link_api(api_host, api_port)
        try:
            async with serve(self.handle_connection, host, port):
                LOG.info("WebSocket server listening on ws://%s:%d", host, port)
                await asyncio.Future()
        finally:
            if self._tick_task:
                self._tick_task.cancel()
            await self._stop_link_api()

    async def _start_link_api(self, host: str, port: int) -> None:
        app = web.Application()
        app.router.add_get("/api/link/sessions", self.http_list_link_sessions)
        app.router.add_post("/api/link/assign", self.http_assign_link_session)

        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        site = web.TCPSite(self._http_runner, host, port)
        await site.start()
        LOG.info("Link API listening on http://%s:%d", host, port)

    async def _stop_link_api(self) -> None:
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None

    async def _periodic_ranking_broadcast(self) -> None:
        while True:
            await asyncio.sleep(5)
            self.prune_expired_pending_sessions()
            await self.broadcast(
                {
                    "type": "ranking_state",
                    "ranking": self.state.leaderboard(),
                }
            )

    async def handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        try:
            join_payload = await websocket.recv()
            data = self.parse_payload(join_payload)
            if data.get("type") != "join":
                await websocket.send(json.dumps({"type": "error", "error": "join message required"}))
                return

            link_via_telegram = bool(data.get("link_via_telegram", False))
            if link_via_telegram:
                device_name = self.sanitize_device_name(str(data.get("device_name", "")))
                session_id = self.register_pending_link_session(websocket, device_name)
                await websocket.send(
                    json.dumps(
                        {
                            "type": "link_required",
                            "session_id": session_id,
                            "device_name": device_name,
                            "message": "Open Telegram and run /link, then choose this session.",
                        }
                    )
                )
            else:
                requested_name = self.sanitize_name(str(data.get("name", "")), "guest")
                await self.register_user_connection(websocket, requested_name)

            async for raw in websocket:
                payload = self.parse_payload(raw)
                await self.handle_message(websocket, payload)
        except ConnectionClosed:
            pass
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Connection error: %s", exc)
        finally:
            self.remove_pending_link_session(websocket)

            disconnected = self.state.users.pop(websocket, None)
            self.telegram_identity_by_ws.pop(websocket, None)
            if disconnected:
                elapsed = int((utc_now() - disconnected.joined_at).total_seconds())
                self.state.accumulated_active_seconds[disconnected.name] += max(0, elapsed)

            was_host = websocket == self.state.host_connection
            if was_host:
                self.state.host_connection = None
                if self.state.ensure_host_connection():
                    await self.broadcast_host_state()

            await self.broadcast({"type": "user_state", "users": self.state.user_snapshots()})
            await self.broadcast({"type": "ranking_state", "ranking": self.state.leaderboard()})

    async def handle_message(self, websocket: WebSocketServerProtocol, payload: dict[str, Any]) -> None:
        msg_type = str(payload.get("type", ""))
        user = self.state.users.get(websocket)

        if not user:
            if msg_type:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "error": "not linked yet. authenticate with Telegram /link first",
                        }
                    )
                )
            return

        if msg_type == "set_mute":
            user.muted = bool(payload.get("muted", False))
            await self.broadcast({"type": "user_state", "users": self.state.user_snapshots()})
            return

        if msg_type == "set_speaking":
            user.speaking = bool(payload.get("speaking", False))
            await self.broadcast({"type": "user_state", "users": self.state.user_snapshots()})
            return

        if msg_type == "token_usage_report":
            raw_tokens = payload.get("token_usage", 0)
            try:
                token_usage = max(0, int(raw_tokens))
            except (TypeError, ValueError):
                token_usage = 0
            self.state.token_usage_by_user[user.name] = token_usage
            await self.broadcast({"type": "ranking_state", "ranking": self.state.leaderboard()})
            return

        if msg_type == "play":
            url = str(payload.get("url", "")).strip()
            if not url:
                await websocket.send(json.dumps({"type": "error", "error": "url required"}))
                return
            if not self.state.host_connection:
                self.state.host_connection = websocket
                await self.broadcast_host_state()
            self.state.enqueue_track(url=url, requested_by=user.name)
            if not self.state.music.url:
                await self.start_next_track()
            else:
                await self.broadcast_queue_state()
            return

        if msg_type == "next":
            self.state.ensure_host_connection()
            if websocket != self.state.host_connection:
                await websocket.send(json.dumps({"type": "error", "error": "only host can use /next"}))
                return
            await self.start_next_track()
            return

        if msg_type == "close":
            self.state.ensure_host_connection()
            if websocket != self.state.host_connection:
                await websocket.send(json.dumps({"type": "error", "error": "only host can use /close"}))
                return
            await self.close_all_sessions(requested_by=user.name)
            return

        if msg_type == "skip":
            await self.handle_skip_request(websocket, user)
            return

        if msg_type == "pause":
            if self.state.music.playing:
                current_position = self.state.music.snapshot()["position_seconds"]
                self.state.music.position_seconds = float(current_position)
                self.state.music.playing = False
                self.state.music.started_at = None
            await self.broadcast({"type": "music_state", "music": self.state.music.snapshot()})
            return

        if msg_type == "resume":
            if self.state.music.url:
                self.state.music.playing = True
                self.state.music.started_at = utc_now()
            await self.broadcast({"type": "music_state", "music": self.state.music.snapshot()})
            return

        if msg_type == "seek":
            position = float(payload.get("position_seconds", 0.0))
            self.state.music.position_seconds = max(0.0, position)
            self.state.music.started_at = utc_now() if self.state.music.playing else None
            await self.broadcast({"type": "music_state", "music": self.state.music.snapshot()})
            return

        if msg_type == "board_update":
            content = str(payload.get("content", "")).strip()
            if not content:
                await websocket.send(json.dumps({"type": "error", "error": "content required"}))
                return
            self.state.board.content = content
            self.state.board.updated_at = utc_now()
            self.state.board.updated_by = user.name
            await self.broadcast({"type": "board_state", "board": self.state.board.snapshot()})
            return

        await websocket.send(json.dumps({"type": "error", "error": f"unknown type: {msg_type}"}))

    async def register_user_connection(self, websocket: WebSocketServerProtocol, name: str) -> None:
        self.state.users[websocket] = UserSession(name=name)
        await self.send_sync_init(websocket)
        await self.broadcast({"type": "user_state", "users": self.state.user_snapshots()})

    async def send_sync_init(self, websocket: WebSocketServerProtocol) -> None:
        await websocket.send(
            json.dumps(
                {
                    "type": "sync_init",
                    "music": self.state.music.snapshot(),
                    "board": self.state.board.snapshot(),
                    "users": self.state.user_snapshots(),
                    "ranking": self.state.leaderboard(),
                    "queue": self.state.queue_snapshots(),
                    "host": self.state.host_name(),
                }
            )
        )

    async def handle_skip_request(self, websocket: WebSocketServerProtocol, user: UserSession) -> None:
        if not self.state.music.url:
            await websocket.send(json.dumps({"type": "error", "error": "no track is currently playing"}))
            return

        self.state.ensure_host_connection()
        host_conn = self.state.host_connection
        if not host_conn or host_conn not in self.state.users:
            await websocket.send(json.dumps({"type": "error", "error": "no host available"}))
            return

        if host_conn == websocket:
            await websocket.send(json.dumps({"type": "system", "message": "you are host; use /next"}))
            return

        try:
            await host_conn.send(
                json.dumps(
                    {
                        "type": "skip_request",
                        "from": user.name,
                        "current_url": self.state.music.url,
                    }
                )
            )
        except Exception:  # noqa: BLE001
            await websocket.send(json.dumps({"type": "error", "error": "failed to notify host"}))
            return

        await websocket.send(
            json.dumps(
                {
                    "type": "system",
                    "message": f"skip request sent to host {self.state.host_name()}",
                }
            )
        )

    async def start_next_track(self) -> None:
        next_item = self.state.pop_next_track()
        if not next_item:
            self.state.music = MusicState()
            await self.broadcast({"type": "music_state", "music": self.state.music.snapshot()})
            await self.broadcast_queue_state()
            return

        self.state.music = MusicState(
            url=next_item.url,
            playing=True,
            position_seconds=0.0,
            started_at=utc_now(),
            requested_by=next_item.requested_by,
        )
        await self.broadcast({"type": "music_state", "music": self.state.music.snapshot()})
        await self.broadcast_queue_state()

    async def broadcast_queue_state(self) -> None:
        await self.broadcast({"type": "queue_state", "queue": self.state.queue_snapshots()})

    async def broadcast_host_state(self) -> None:
        await self.broadcast({"type": "host_state", "host": self.state.host_name()})

    async def close_all_sessions(self, requested_by: str) -> None:
        report_path = self.write_settlement_report(requested_by=requested_by)

        await self.broadcast(
            {
                "type": "system",
                "message": f"host requested /close. session closed. report={report_path}",
            }
        )

        active_connections = list(self.state.users.keys())
        pending_connections = [pending.websocket for pending in self.pending_link_sessions.values()]
        all_connections = list({*active_connections, *pending_connections})

        self.pending_link_sessions.clear()
        self.pending_by_ws.clear()

        for connection in all_connections:
            try:
                await connection.close(code=4100, reason="session closed by host")
            except Exception:  # noqa: BLE001
                continue

    def write_settlement_report(self, requested_by: str) -> str:
        os.makedirs(self.reports_dir, exist_ok=True)

        now = utc_now()
        leaderboard = self.state.leaderboard()
        users = self.state.user_snapshots()
        report_payload = {
            "generated_at": now.isoformat(),
            "requested_by": requested_by,
            "host": self.state.host_name(),
            "members": users,
            "leaderboard": leaderboard,
            "music": self.state.music.snapshot(),
            "queue": self.state.queue_snapshots(),
            "board": self.state.board.snapshot(),
        }

        filename = f"settlement-{now.strftime('%Y%m%d-%H%M%S')}.json"
        path = os.path.join(self.reports_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report_payload, handle, ensure_ascii=False, indent=2)

        return path

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self.state.users:
            return
        data = json.dumps(message)
        tasks = [conn.send(data) for conn in self.state.users]
        await asyncio.gather(*tasks, return_exceptions=True)

    def register_pending_link_session(self, websocket: WebSocketServerProtocol, device_name: str) -> str:
        self.prune_expired_pending_sessions()

        session_id = ""
        for _ in range(8):
            candidate = secrets.token_hex(3).upper()
            if candidate not in self.pending_link_sessions:
                session_id = candidate
                break
        if not session_id:
            session_id = secrets.token_hex(4).upper()

        pending = PendingLinkSession(
            session_id=session_id,
            websocket=websocket,
            device_name=device_name,
        )
        self.pending_link_sessions[session_id] = pending
        self.pending_by_ws[websocket] = session_id
        LOG.info("Pending link session created: %s (%s)", session_id, device_name)
        return session_id

    def remove_pending_link_session(self, websocket: WebSocketServerProtocol) -> None:
        session_id = self.pending_by_ws.pop(websocket, None)
        if not session_id:
            return
        self.pending_link_sessions.pop(session_id, None)

    def prune_expired_pending_sessions(self) -> None:
        to_remove: list[str] = []
        for session_id, pending in self.pending_link_sessions.items():
            age = int((utc_now() - pending.created_at).total_seconds())
            ws_closed = pending.websocket.closed
            if age > self.link_ttl_seconds or ws_closed:
                to_remove.append(session_id)

        for session_id in to_remove:
            pending = self.pending_link_sessions.pop(session_id, None)
            if pending:
                self.pending_by_ws.pop(pending.websocket, None)

    async def assign_pending_link_session(
        self,
        session_id: str,
        telegram_user_id: str,
        telegram_display_name: str,
    ) -> tuple[bool, str | dict[str, Any]]:
        self.prune_expired_pending_sessions()
        pending = self.pending_link_sessions.get(session_id)
        if not pending:
            return False, "session not found or expired"

        websocket = pending.websocket
        if websocket.closed:
            self.remove_pending_link_session(websocket)
            return False, "session is disconnected"

        final_name = self.sanitize_name(
            telegram_display_name,
            fallback=f"tg-{telegram_user_id}",
        )

        self.remove_pending_link_session(websocket)
        self.telegram_identity_by_ws[websocket] = str(telegram_user_id)
        await websocket.send(
            json.dumps(
                {
                    "type": "link_success",
                    "name": final_name,
                    "telegram_user_id": str(telegram_user_id),
                }
            )
        )
        await self.register_user_connection(websocket, final_name)

        return True, {
            "linked": True,
            "session_id": session_id,
            "name": final_name,
            "telegram_user_id": str(telegram_user_id),
        }

    async def http_list_link_sessions(self, request: web.Request) -> web.Response:
        if not self.is_api_authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        self.prune_expired_pending_sessions()
        sessions = [
            pending.snapshot()
            for pending in sorted(
                self.pending_link_sessions.values(),
                key=lambda item: item.created_at,
            )
        ]
        return web.json_response({"sessions": sessions})

    async def http_assign_link_session(self, request: web.Request) -> web.Response:
        if not self.is_api_authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid payload"}, status=400)

        session_id = str(payload.get("session_id", "")).strip().upper()
        telegram_user_id = str(payload.get("telegram_user_id", "")).strip()
        telegram_display_name = str(payload.get("telegram_display_name", "")).strip()

        if not session_id or not telegram_user_id:
            return web.json_response(
                {"error": "session_id and telegram_user_id are required"},
                status=400,
            )

        ok, result = await self.assign_pending_link_session(
            session_id=session_id,
            telegram_user_id=telegram_user_id,
            telegram_display_name=telegram_display_name,
        )
        if not ok:
            return web.json_response({"error": str(result)}, status=404)

        return web.json_response(result)

    def is_api_authorized(self, request: web.Request) -> bool:
        if not self.link_api_token:
            return True
        provided = request.headers.get("Authorization", "")
        expected = f"Bearer {self.link_api_token}"
        return secrets.compare_digest(provided, expected)

    @staticmethod
    def parse_payload(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
        return {}

    @staticmethod
    def sanitize_name(name: str, fallback: str) -> str:
        cleaned = " ".join(name.split()).strip()
        if not cleaned:
            cleaned = fallback
        return cleaned[:32]

    @staticmethod
    def sanitize_device_name(name: str) -> str:
        cleaned = " ".join(name.split()).strip()
        if not cleaned:
            return "unknown-device"
        return cleaned[:48]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scratch House signaling/state server")
    parser.add_argument("--host", default="0.0.0.0", help="WebSocket bind host")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket bind port")
    parser.add_argument("--api-host", default="127.0.0.1", help="Link API bind host")
    parser.add_argument("--api-port", type=int, default=8787, help="Link API bind port (0 to disable)")
    parser.add_argument("--link-api-token", default="", help="Bearer token for Link API")
    parser.add_argument("--link-ttl-seconds", type=int, default=120, help="Pending link TTL in seconds")
    parser.add_argument("--reports-dir", default="reports", help="Directory to write settlement reports")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    server = ScratchHouseServer(
        link_api_token=str(args.link_api_token),
        link_ttl_seconds=int(args.link_ttl_seconds),
        reports_dir=str(args.reports_dir),
    )
    try:
        asyncio.run(server.start(args.host, args.port, args.api_host, args.api_port))
    except KeyboardInterrupt:
        LOG.info("Server stopped")


if __name__ == "__main__":
    main()
