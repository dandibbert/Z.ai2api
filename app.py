#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Z.ai 2 API
将 Z.ai 代理为 OpenAI/Anthropic Compatible 格式，支持免令牌、智能处理思考链、图片上传（仅登录后）等功能
基于 https://github.com/kbykb/OpenAI-Compatible-API-Proxy-for-Z 使用 AI 辅助重构。
"""

import os, re, json, base64, urllib.parse, requests, hashlib, hmac, uuid, traceback, logging, time, mimetypes
from collections import defaultdict, deque
from datetime import datetime
from threading import Lock
from flask import Flask, request, Response, jsonify, make_response, redirect, render_template_string, session, url_for, g, has_request_context
from typing import Any, Dict, List, Union, Optional

from dotenv import load_dotenv
load_dotenv()

# 配置
_raw_protocol = str(os.getenv("PROTOCOL", "")).strip()
_raw_base = str(os.getenv("BASE", "chat.z.ai")).strip()
_parsed_base = urllib.parse.urlparse(_raw_base) if _raw_base.startswith(("http://", "https://")) else None
_base_protocol = f"{_parsed_base.scheme}:" if _parsed_base and _parsed_base.scheme else (_raw_protocol if _raw_protocol.endswith(":") else (f"{_raw_protocol}:" if _raw_protocol else "https:"))
_base_host = (_parsed_base.netloc or _parsed_base.path) if _parsed_base else _raw_base
_base_host = _base_host.rstrip("/") or "chat.z.ai"


class cfg:
        class source:
                protocol = _base_protocol
                host = _base_host
                token = str(os.getenv("TOKEN", "")).strip()
        class api:
                port = int(os.getenv("PORT", "8080"))
                debug = str(os.getenv("DEBUG", "false")).lower() == "true"
                debug_msg = str(os.getenv("DEBUG_MSG", "false")).lower() == "true"
                think = str(os.getenv("THINK_TAGS_MODE", "reasoning"))
                anon = str(os.getenv("ANONYMOUS_MODE", "true")).lower() == "true"
        class model:
                default = str(os.getenv("MODEL", "glm-4.6"))
                mapping = {}


BASE_URL = f"{cfg.source.protocol}//{cfg.source.host}"
AUTH_TOKEN = str(os.getenv("AUTH_TOKEN", "")).strip()
SECRET_KEY = str(os.getenv("SECRET_KEY", "")).strip() or "zai2api-dashboard"

RAW_TOKEN_POOL = str(os.getenv("TOKEN_POOL", "")).strip()
TOKEN_POOL_FAILURE_THRESHOLD = int(os.getenv("TOKEN_POOL_FAILURE_THRESHOLD", "3"))
TOKEN_POOL_RESET_FAILURES = int(os.getenv("TOKEN_POOL_RESET_FAILURES", "1800"))

STATE_DIR = os.getenv(
        "ZAI2API_STATE_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
TOKEN_POOL_STATE_FILE = os.path.join(STATE_DIR, "token_pool.json")


def _ensure_state_dir():
        try:
                os.makedirs(STATE_DIR, exist_ok=True)
        except Exception:
                logging.getLogger(__name__).warning("无法创建状态目录: %s", STATE_DIR)


def _load_persisted_state() -> Dict[str, Any]:
        try:
                with open(TOKEN_POOL_STATE_FILE, "r", encoding="utf-8") as fh:
                        payload = json.load(fh)
                        tokens = payload.get("tokens", [])
                        if isinstance(tokens, list):
                                payload["tokens"] = [
                                        str(token).strip() for token in tokens if str(token).strip()
                                ]
                        else:
                                payload["tokens"] = []
                        if not isinstance(payload.get("salt"), str):
                                payload["salt"] = ""
                        return payload
        except FileNotFoundError:
                return {"tokens": [], "salt": ""}
        except Exception as exc:
                logging.getLogger(__name__).warning("读取持久化 token 池失败: %s", exc)
        return {"tokens": [], "salt": ""}


def _persist_token_pool(tokens: List[str]):
        try:
                _ensure_state_dir()
                with open(TOKEN_POOL_STATE_FILE, "w", encoding="utf-8") as fh:
                        json.dump({"tokens": tokens, "salt": TOKEN_HASH_SECRET}, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
                logging.getLogger(__name__).warning("写入持久化 token 池失败: %s", exc)


def _parse_token_pool(raw: str) -> List[str]:
        tokens: List[str] = []
        if not raw:
                return tokens
        for candidate in re.split(r"[\n,]", raw):
                token = candidate.strip()
                if token:
                        tokens.append(token)
        return tokens


def _normalize_token_inputs(values: Any) -> List[str]:
        if values is None:
                return []
        if isinstance(values, str):
                sources = [values]
        elif isinstance(values, list):
                sources = [str(item) for item in values]
        else:
                return []
        tokens: List[str] = []
        for source in sources:
                for candidate in re.split(r"[\n,]", source):
                        token = candidate.strip()
                        if token:
                                tokens.append(token)
        return tokens


class TokenPool:
        def __init__(self, tokens: Optional[List[str]] = None, failure_threshold: int = 3):
                self._tokens: List[str] = []
                self._lock = Lock()
                self._index = 0
                self._failure_threshold = max(1, failure_threshold)
                self._failures: Dict[str, int] = {}
                self._disabled: Dict[str, datetime] = {}
                self._successes: Dict[str, int] = {}
                self._token_ids: Dict[str, str] = {}
                if tokens:
                        self.update(tokens)

        def _available_tokens(self) -> List[str]:
                now = datetime.now()
                tokens: List[str] = []
                for token in self._tokens:
                        disabled_at = self._disabled.get(token)
                        if disabled_at and (now - disabled_at).total_seconds() < TOKEN_POOL_RESET_FAILURES:
                                continue
                        tokens.append(token)
                return tokens

        def get(self) -> Optional[str]:
                with self._lock:
                        available = self._available_tokens()
                        if not available:
                                self._disabled.clear()
                                available = self._available_tokens()
                        if not available:
                                return None
                        token = available[self._index % len(available)]
                        self._index = (self._index + 1) % len(available)
                        return token

        def mark_success(self, token: Optional[str]):
                        if not token or token not in self._tokens:
                                return
                        with self._lock:
                                self._successes[token] = self._successes.get(token, 0) + 1
                                self._failures.pop(token, None)
                                self._disabled.pop(token, None)

        def mark_failure(self, token: Optional[str]):
                if not token or token not in self._tokens:
                        return
                with self._lock:
                        count = self._failures.get(token, 0) + 1
                        self._failures[token] = count
                        self._successes.setdefault(token, 0)
                        if count >= self._failure_threshold:
                                self._disabled[token] = datetime.now()

        def update(self, tokens: List[str]):
                unique = []
                seen = set()
                for token in tokens:
                        if token and token not in seen:
                                unique.append(token)
                                seen.add(token)
                with self._lock:
                        self._tokens = unique
                        self._index = 0
                        self._token_ids = {_token_identifier(token): token for token in self._tokens}
                        for token in list(self._failures.keys()):
                                if token not in self._tokens:
                                        self._failures.pop(token, None)
                                        self._disabled.pop(token, None)
                                        self._successes.pop(token, None)
                        for token in self._tokens:
                                self._successes.setdefault(token, 0)
                                self._failures.setdefault(token, 0)

        def contains(self, token: Optional[str]) -> bool:
                return bool(token and token in self._tokens)

        def tokens(self) -> List[str]:
                with self._lock:
                        return list(self._tokens)

        def resolve_id(self, token_id: str) -> Optional[str]:
                if not token_id:
                        return None
                with self._lock:
                        return self._token_ids.get(token_id)

        def snapshot(self) -> Dict[str, Any]:
                with self._lock:
                        now = datetime.now()
                        available = set(self._available_tokens())
                        items: List[Dict[str, Any]] = []
                        for idx, token in enumerate(self._tokens):
                                disabled_at = self._disabled.get(token)
                                cooldown_seconds = 0
                                if disabled_at:
                                        elapsed = (now - disabled_at).total_seconds()
                                        remaining = TOKEN_POOL_RESET_FAILURES - elapsed
                                        if remaining > 0:
                                                cooldown_seconds = int(max(0, round(remaining)))
                                token_id = _token_identifier(token)
                                items.append({
                                        "token_id": token_id,
                                        "display": _token_display_from_id(token_id),
                                        "index": idx,
                                        "failures": self._failures.get(token, 0),
                                        "successes": self._successes.get(token, 0),
                                        "disabled": token not in available,
                                        "cooldown_seconds": cooldown_seconds,
                                        "last_disabled_at": disabled_at.isoformat() if disabled_at else None,
                                })
                        return {
                                "size": len(self._tokens),
                                "next_index": self._index,
                                "tokens": items,
                        }


persisted_state = _load_persisted_state()
_persisted_tokens = persisted_state.get("tokens", [])
_persisted_salt = persisted_state.get("salt", "")

TOKEN_HASH_SECRET = (
        str(os.getenv("TOKEN_HASH_SECRET", "")).strip()
        or (_persisted_salt if isinstance(_persisted_salt, str) else "")
)
if not TOKEN_HASH_SECRET:
        TOKEN_HASH_SECRET = base64.urlsafe_b64encode(os.urandom(24)).decode("utf-8").rstrip("=")


def _token_identifier(token: str) -> str:
        if not token:
                return ""
        return hmac.new(
                TOKEN_HASH_SECRET.encode("utf-8"),
                token.encode("utf-8"),
                hashlib.sha256,
        ).hexdigest()


def _token_display_from_id(token_id: str) -> str:
        if not token_id:
                return "token"
        return f"token:{token_id[:8]}"


TOKEN_POOL_TOKENS = _parse_token_pool(RAW_TOKEN_POOL)
for token in _persisted_tokens:
        if token not in TOKEN_POOL_TOKENS:
                TOKEN_POOL_TOKENS.append(token)
if cfg.source.token and cfg.source.token not in TOKEN_POOL_TOKENS:
        TOKEN_POOL_TOKENS.append(cfg.source.token)

token_pool = TokenPool(TOKEN_POOL_TOKENS, TOKEN_POOL_FAILURE_THRESHOLD)
_persist_token_pool(token_pool.tokens())


def _update_token_pool(tokens: List[str]):
        token_pool.update(tokens)
        _persist_token_pool(token_pool.tokens())


def _client_ip() -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
                return forwarded.split(",")[0].strip()
        return request.remote_addr or ""


def _format_upstream_path(url: str) -> str:
        if not url:
                return ""
        parsed = urllib.parse.urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
                path = f"{path}?{parsed.query}"
        if parsed.netloc:
                return f"{parsed.netloc}{path}"
        return url


class RequestMetrics:
        def __init__(self):
                self._lock = Lock()
                self._total_requests = 0
                self._success_requests = 0
                self._failure_requests = 0
                self._total_response_time = 0.0
                self._recent_requests: deque = deque(maxlen=100)
                self._token_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"success": 0, "failure": 0, "source": "unknown"})

        def record(self, *, method: str, path: str, status_code: Optional[int], duration: float, client_ip: str, token_info: Optional[Dict[str, Any]], error: Optional[str] = None):
                success = status_code is not None and 200 <= status_code < 400
                token_source = "none"
                token_value = None
                token_id = None
                token_display = ""
                if token_info:
                        token_source = token_info.get("source", "none")
                        token_value = token_info.get("token")
                if token_source == "anonymous" or not token_value:
                        token_display = "匿名 token"
                        token_id = None
                elif token_source in ("pool", "static"):
                        token_id = _token_identifier(token_value)
                        token_display = _token_display_from_id(token_id)
                else:
                        token_display = token_value or ""

                key = token_id if token_id else f"__{token_source}__"
                with self._lock:
                        self._total_requests += 1
                        self._total_response_time += duration
                        if success:
                                self._success_requests += 1
                                self._token_stats[key]["success"] += 1
                        else:
                                self._failure_requests += 1
                                self._token_stats[key]["failure"] += 1
                        self._token_stats[key]["source"] = token_source
                        if token_display:
                                self._token_stats[key]["display"] = token_display
                        entry = {
                                "timestamp": datetime.now().isoformat(),
                                "method": method,
                                "path": path,
                                "status_code": status_code,
                                "duration_ms": int(duration * 1000),
                                "client_ip": client_ip,
                                "token": token_display,
                        }
                        if error:
                                entry["error"] = error
                        self._recent_requests.appendleft(entry)
                        return entry

        def snapshot(self) -> Dict[str, Any]:
                with self._lock:
                        average = self._total_response_time / self._total_requests if self._total_requests else 0.0
                        recent = list(self._recent_requests)
                        token_stats = []
                        for key, stats in self._token_stats.items():
                                token_id = key if not key.startswith("__") else None
                                source = stats.get("source", "unknown")
                                if not token_id and source == "anonymous":
                                        display = "匿名 token"
                                elif token_id:
                                        display = stats.get("display") or "Token"
                                else:
                                        display = source
                                token_stats.append({
                                        "token_id": token_id,
                                        "display": display,
                                        "source": source,
                                        "success": stats.get("success", 0),
                                        "failure": stats.get("failure", 0),
                                })
                        return {
                                "total_requests": self._total_requests,
                                "success_requests": self._success_requests,
                                "failure_requests": self._failure_requests,
                                "average_response_time": round(average * 1000, 2),
                                "recent_requests": recent,
                                "token_stats": token_stats,
                        }


request_metrics = RequestMetrics()


def _record_upstream_metrics(*, method: str, url: str, status_code: Optional[int], duration: float, error: Optional[str] = None, token_info: Optional[Dict[str, Any]] = None):
        try:
                client_ip = _client_ip()
        except Exception:
                client_ip = ""
        if token_info is None and has_request_context():
                token_info = getattr(g, "current_token_info", None)
        path = _format_upstream_path(url)
        return request_metrics.record(
                method=method,
                path=path,
                status_code=status_code,
                duration=duration,
                client_ip=client_ip,
                token_info=token_info,
                error=error,
        )


def _finalize_upstream_response(response, *, error: Optional[str] = None):
        context = getattr(response, "_metrics_context", None)
        if not context or context.get("finalized"):
                return
        context["finalized"] = True
        start_time = context.get("start_time")
        url = context.get("url") or getattr(response, "url", "")
        method = context.get("method", "POST")
        duration = time.perf_counter() - start_time if start_time else 0.0
        status_code = response.status_code if error is None else None
        token_info = context.get("token_info")
        _record_upstream_metrics(
                method=method,
                url=url,
                status_code=status_code,
                duration=duration,
                error=error,
                token_info=token_info,
        )
        token = context.get("token")
        if token_pool.contains(token):
                success = error is None and status_code is not None and 200 <= status_code < 400
                if success:
                        token_pool.mark_success(token)
                else:
                        token_pool.mark_failure(token)


def _has_dashboard_session() -> bool:
        if not AUTH_TOKEN:
                return True
        return bool(session.get("authenticated"))


def _establish_dashboard_session():
        session.clear()
        session.permanent = False
        session["authenticated"] = True
        session.modified = True


def _clear_dashboard_session():
        session.clear()
        session.modified = True


def _extract_auth_token() -> str:
        auth_header = request.headers.get("Authorization", "").strip()
        candidate = ""
        if auth_header:
                if auth_header.lower().startswith("bearer "):
                        candidate = auth_header[7:].strip()
                else:
                        candidate = auth_header
        if not candidate:
                candidate = request.headers.get("X-Auth-Token", "").strip()
        if not candidate:
                candidate = request.args.get("auth_token", "").strip()
        if not candidate:
                candidate = request.args.get("token", "").strip()
        return candidate


def _has_valid_api_auth() -> bool:
        if not AUTH_TOKEN:
                return True
        if session.get("authenticated"):
                return True
        candidate = _extract_auth_token()
        return candidate == AUTH_TOKEN


def _require_api_auth():
        if _has_valid_api_auth():
                return None
        response = jsonify({"error": "unauthorized"})
        return utils.request.response(make_response(response, 401))


def _require_dashboard_auth():
        if _has_dashboard_session() or _has_valid_api_auth():
                return None
        response = jsonify({"error": "unauthorized"})
        return utils.request.response(make_response(response, 401))


def _build_dashboard_payload() -> Dict[str, Any]:
        metrics_snapshot = request_metrics.snapshot()
        token_snapshot = token_pool.snapshot()
        return {
                "anonymous_mode": cfg.api.anon,
                "stats": {
                        "total_requests": metrics_snapshot.get("total_requests", 0),
                        "success_requests": metrics_snapshot.get("success_requests", 0),
                        "failure_requests": metrics_snapshot.get("failure_requests", 0),
                        "average_response_time": metrics_snapshot.get("average_response_time", 0),
                },
                "recent_requests": metrics_snapshot.get("recent_requests", []),
                "token_stats": metrics_snapshot.get("token_stats", []),
                "token_pool": token_snapshot,
        }


STATUS_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Z.ai 2 API 状态</title>
    <style>
        :root { color-scheme: dark; }
        body {
            margin: 0;
            font-family: 'Inter', 'PingFang SC', system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at top, rgba(56, 189, 248, 0.15), transparent 50%), #020617;
            color: #e2e8f0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .card {
            background: rgba(15, 23, 42, 0.9);
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 16px;
            padding: 32px;
            max-width: 520px;
            width: 90%;
            box-shadow: 0 24px 48px rgba(15, 23, 42, 0.35);
        }
        h1 { margin: 0 0 16px 0; font-size: 28px; }
        .status { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px; background: rgba(34, 197, 94, 0.2); color: #4ade80; font-weight: 600; }
        .meta { margin: 12px 0; font-size: 14px; color: #94a3b8; }
        .meta span { display: block; margin-bottom: 6px; }
        a { color: #38bdf8; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class=\"card\">
        <h1>Z.ai 2 API</h1>
        <div class=\"status\">
            <span>●</span>
            <span>在线</span>
        </div>
        <div class=\"meta\">
            <span>上游：{{ upstream }}</span>
            <span>端口：{{ port }}</span>
            <span>匿名模式：{{ '启用' if anonymous_mode else '关闭' }}</span>
        </div>
        <a href=\"/dashboard\">打开监控面板 →</a>
    </div>
</body>
</html>
"""


DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Z.ai 2 API 仪表盘</title>
    <style>
        :root { color-scheme: dark; }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: 'Inter', 'PingFang SC', system-ui, -apple-system, sans-serif;
            background: #020617;
            color: #e2e8f0;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 32px 32px 16px 32px;
        }
        header h1 {
            margin: 0;
            font-size: 28px;
            font-weight: 600;
            letter-spacing: -0.02em;
        }
        .tag {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            border-radius: 999px;
            background: var(--accent-muted);
            color: var(--accent);
            font-size: 14px;
        }
        header .actions {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        button {
            cursor: pointer;
            border: none;
            border-radius: 8px;
            padding: 8px 16px;
            font-size: 14px;
            font-weight: 500;
            background: var(--accent);
            color: #0f172a;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }
        button:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(34, 211, 238, 0.35); }
        button.ghost {
            background: transparent;
            color: var(--text);
            border: 1px solid var(--border);
        }
        main { padding: 0 32px 40px 32px; }
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 20px;
            backdrop-filter: blur(12px);
            box-shadow: 0 16px 36px rgba(15, 23, 42, 0.45);
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-top: 16px;
        }
        .stat-card h3 {
            margin: 0;
            font-size: 14px;
            color: var(--muted);
            font-weight: 500;
        }
        .stat-card p {
            margin: 8px 0 0;
            font-size: 26px;
            font-weight: 600;
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
        }
        .section-header h2 {
            margin: 0;
            font-size: 20px;
        }
        .table-wrapper {
            overflow: auto;
            border-radius: 12px;
            border: 1px solid var(--border);
            background: rgba(15, 23, 42, 0.65);
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid rgba(148, 163, 184, 0.1);
        }
        th {
            background: rgba(30, 41, 59, 0.65);
            color: var(--muted);
            font-weight: 500;
        }
        tbody tr:hover {
            background: rgba(148, 163, 184, 0.08);
        }
        .badge {
            display: inline-flex;
            align-items: center;
            padding: 4px 8px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 500;
        }
        .badge.success { background: rgba(52, 211, 153, 0.16); color: var(--success); }
        .badge.danger { background: rgba(248, 113, 113, 0.16); color: var(--danger); }
        form#add-token-form {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            align-items: flex-start;
        }
        form#add-token-form textarea {
            flex: 1 1 320px;
            min-height: 96px;
            padding: 10px 14px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: rgba(15, 23, 42, 0.65);
            color: var(--text);
            font-size: 14px;
            resize: vertical;
            line-height: 1.5;
        }
        .help-text { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
        @media (max-width: 768px) {
            header, main { padding: 24px 20px; }
            header { flex-direction: column; align-items: flex-start; }
            .section-header { flex-direction: column; align-items: flex-start; }
            form#add-token-form { width: 100%; }
            form#add-token-form textarea { width: 100%; }
        }
    </style>
</head>
<body>
    <header>
        <h1>Z.ai 2 API 监控面板</h1>
        <div class=\"actions\">
            <span id=\"mode-indicator\" class=\"tag\">{{ '匿名模式' if anonymous_mode else '非匿名模式' }}</span>
            <button id=\"logout-btn\" class=\"ghost\" type=\"button\">退出登录</button>
        </div>
    </header>
    <main>
        <section class=\"card\">
            <div class=\"section-header\">
                <h2>请求统计</h2>
                <div class=\"actions\">
                    <label style=\"display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);\">
                        <input type=\"checkbox\" id=\"auto-refresh\" checked /> 自动刷新 (5s)
                    </label>
                    <span id=\"last-updated\" style=\"font-size:12px;color:var(--muted);\"></span>
                </div>
            </div>
            <div class=\"stats-grid\">
                <div class=\"stat-card\">
                    <h3>总请求数</h3>
                    <p id=\"stat-total\">0</p>
                </div>
                <div class=\"stat-card\">
                    <h3>成功请求</h3>
                    <p id=\"stat-success\">0</p>
                </div>
                <div class=\"stat-card\">
                    <h3>失败请求</h3>
                    <p id=\"stat-failure\">0</p>
                </div>
                <div class=\"stat-card\">
                    <h3>平均响应时间 (ms)</h3>
                    <p id=\"stat-average\">0</p>
                </div>
            </div>
        </section>
        <section style=\"margin-top:24px;\">
            <div class=\"section-header\">
                <h2>最近 100 条请求</h2>
            </div>
            <div class=\"table-wrapper\">
                <table id=\"requests-table\">
                    <thead>
                        <tr>
                            <th>时间</th>
                            <th>方法</th>
                            <th>路径</th>
                            <th>状态码</th>
                            <th>耗时 (ms)</th>
                            <th>客户端 IP</th>
                            <th>令牌</th>
                        </tr>
                    </thead>
                    <tbody></tbody>
                </table>
            </div>
        </section>
        <section style=\"margin-top:24px;\">
            <div class=\"section-header\">
                <h2>Token 管理</h2>
                <form id=\"add-token-form\" method=\"post\" action=\"/dashboard/api/tokens\">
                    <textarea id=\"new-token\" name=\"token\" placeholder=\"粘贴新 Token（支持逗号或换行批量添加）\" autocomplete=\"off\" required spellcheck=\"false\"></textarea>
                    <button type=\"submit\">添加 Token</button>
                </form>
            </div>
            <p class=\"help-text\">成功/失败次数基于通过代理发出的请求统计。支持一次粘贴多个 Token（使用逗号或换行分隔），所有改动都会自动持久化。</p>
            <div class=\"table-wrapper\">
                <table id=\"tokens-table\">
                    <thead>
                        <tr>
                            <th>Token</th>
                            <th>来源</th>
                            <th>请求成功</th>
                            <th>请求失败</th>
                            <th>禁用状态</th>
                            <th>冷却 (秒)</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody></tbody>
                </table>
            </div>
        </section>
    </main>
    <script type=\"application/json\" id=\"dashboard-initial-data\">{{ initial_data|safe }}</script>
    <script>
    (() => {
        const autoRefreshToggle = document.getElementById('auto-refresh');
        const lastUpdated = document.getElementById('last-updated');
        const modeIndicator = document.getElementById('mode-indicator');
        const totalEl = document.getElementById('stat-total');
        const successEl = document.getElementById('stat-success');
        const failureEl = document.getElementById('stat-failure');
        const averageEl = document.getElementById('stat-average');
        const requestsBody = document.querySelector('#requests-table tbody');
        const tokensBody = document.querySelector('#tokens-table tbody');
        const addTokenForm = document.getElementById('add-token-form');
        const newTokenInput = document.getElementById('new-token');
        const logoutBtn = document.getElementById('logout-btn');
        const defaultAnonymousMode = {{ 'true' if anonymous_mode else 'false' }};
        const initialDataEl = document.getElementById('dashboard-initial-data');
        let initialData = null;
        let timer = null;
        let useAjaxSubmission = true;

        if (initialDataEl) {
            try {
                initialData = JSON.parse(initialDataEl.textContent || '{}');
            } catch (err) {
                console.error('解析初始数据失败', err);
                initialData = null;
            }
        }

        function formatNumber(value) {
            return (value ?? 0).toLocaleString('zh-CN');
        }

        function formatTimestamp(ts) {
            try {
                return new Date(ts).toLocaleString();
            } catch (_) {
                return ts;
            }
        }

        function applyOverview(data) {
            const payload = data || initialData || {};
            const stats = payload.stats || {};
            const recentRequests = payload.recent_requests || [];
            const tokenStats = payload.token_stats || [];
            const pool = payload.token_pool || { tokens: [] };

            totalEl.textContent = formatNumber(stats.total_requests);
            successEl.textContent = formatNumber(stats.success_requests);
            failureEl.textContent = formatNumber(stats.failure_requests);
            averageEl.textContent = formatNumber(stats.average_response_time);

            requestsBody.innerHTML = recentRequests.map(item => `
                <tr>
                    <td>${formatTimestamp(item.timestamp)}</td>
                    <td>${item.method}</td>
                    <td>${item.path}</td>
                    <td>${item.status_code ?? '-'}</td>
                    <td>${item.duration_ms ?? 0}</td>
                    <td>${item.client_ip || '-'}</td>
                    <td>${item.token || '-'}</td>
                </tr>
            `).join('');

            tokensBody.innerHTML = (pool.tokens || []).map(item => `
                <tr>
                    <td>${item.display}</td>
                    <td>${(tokenStats.find(stat => stat.token_id === item.token_id) || {}).source || '-'}</td>
                    <td>${(tokenStats.find(stat => stat.token_id === item.token_id) || {}).success || 0}</td>
                    <td>${(tokenStats.find(stat => stat.token_id === item.token_id) || {}).failure || 0}</td>
                    <td>${item.disabled ? '<span class="badge danger">禁用</span>' : '<span class="badge success">正常</span>'}</td>
                    <td>${item.cooldown_seconds ?? 0}</td>
                    <td>
                        <button type="button" data-token-id="${item.token_id}">移除</button>
                    </td>
                </tr>
            `).join('');

            modeIndicator.textContent = payload.anonymous_mode ? '匿名模式' : '非匿名模式';
        }

        async function fetchOverview() {
            try {
                const res = await fetch('/dashboard/api/overview', { credentials: 'include' });
                if (res.status === 401) {
                    window.location.href = '/dashboard';
                    return;
                }
                const data = await res.json();
                applyOverview(data);
                lastUpdated.textContent = `更新于 ${new Date().toLocaleTimeString()}`;
            } catch (err) {
                console.error('刷新仪表盘失败', err);
                lastUpdated.textContent = '刷新失败，稍后自动重试';
                applyOverview(null);
            }
        }

        function scheduleRefresh(enabled) {
            if (timer) {
                clearInterval(timer);
                timer = null;
            }
            if (enabled) {
                timer = setInterval(fetchOverview, 5000);
            }
        }

        autoRefreshToggle.addEventListener('change', (event) => {
            const enabled = event.target.checked;
            scheduleRefresh(enabled);
        });

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                scheduleRefresh(false);
            } else if (autoRefreshToggle.checked) {
                fetchOverview();
                scheduleRefresh(true);
            }
        });

        addTokenForm.addEventListener('submit', async (event) => {
            if (!useAjaxSubmission) {
                return;
            }
            event.preventDefault();
            const tokens = newTokenInput.value
                .replace(/\\n/g, ' ')
                .replace(/\\r/g, ' ')
                .replace(/\\t/g, ' ')
                .replace(/,/g, ' ')
                .split(' ')
                .map(item => item.trim())
                .filter(Boolean);
            if (!tokens.length) return;
            try {
                const res = await fetch('/dashboard/api/tokens', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({ tokens }),
                });
                if (res.status === 401) {
                    window.location.href = '/dashboard';
                    return;
                }
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                newTokenInput.value = '';
                const data = await res.json();
                applyOverview(data);
                lastUpdated.textContent = `更新于 ${new Date().toLocaleTimeString()}`;
            } catch (err) {
                console.error('添加 Token 失败', err);
                alert('添加失败，尝试使用表单回退提交。');
                useAjaxSubmission = false;
                addTokenForm.submit();
            }
        });

        tokensBody.addEventListener('click', async (event) => {
            const button = event.target.closest('button[data-token-id]');
            if (!button) return;
            const tokenId = button.getAttribute('data-token-id');
            if (!confirm('确认移除该 Token 吗？')) return;
            try {
                const res = await fetch('/dashboard/api/tokens', {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({ token_id: tokenId }),
                });
                if (res.status === 401) {
                    window.location.href = '/dashboard';
                    return;
                }
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                applyOverview(data);
                lastUpdated.textContent = `更新于 ${new Date().toLocaleTimeString()}`;
            } catch (err) {
                console.error('移除 Token 失败', err);
                alert('移除失败，请重试');
            }
        });

        if (logoutBtn) {
            logoutBtn.addEventListener('click', async () => {
                try {
                    await fetch('/dashboard/logout', { method: 'POST', credentials: 'include' });
                } finally {
                    window.location.href = '/dashboard';
                }
            });
        }

        if (initialData) {
            applyOverview(initialData);
            lastUpdated.textContent = '已加载当前快照';
        } else {
            applyOverview(null);
        }

        fetchOverview();
        scheduleRefresh(true);
    })();
    </script>
</body>
</html>
"""


DASHBOARD_LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>登录仪表盘</title>
    <style>
        :root { color-scheme: dark; }
        body {
            margin: 0;
            font-family: 'Inter', 'PingFang SC', system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at top, rgba(34, 211, 238, 0.18), transparent 55%), #020617;
            color: #e2e8f0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .card {
            background: rgba(15, 23, 42, 0.92);
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 18px;
            padding: 36px;
            width: min(420px, 92%);
            box-shadow: 0 28px 60px rgba(15, 23, 42, 0.45);
        }
        h1 {
            margin: 0;
            font-size: 26px;
            font-weight: 600;
            letter-spacing: -0.02em;
        }
        p {
            margin: 12px 0 24px;
            font-size: 15px;
            color: #94a3b8;
        }
        input[type="password"], input[type="text"] {
            width: 100%;
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid rgba(148, 163, 184, 0.25);
            background: rgba(15, 23, 42, 0.65);
            color: inherit;
            font-size: 15px;
            transition: border 0.2s ease;
        }
        input:focus {
            outline: none;
            border-color: rgba(56, 189, 248, 0.65);
            box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
        }
        button {
            width: 100%;
            margin-top: 18px;
            padding: 12px;
            border: none;
            border-radius: 12px;
            background: linear-gradient(135deg, #38bdf8, #22d3ee);
            color: #0f172a;
            font-weight: 600;
            font-size: 15px;
            cursor: pointer;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }
        button:hover {
            transform: translateY(-1px);
            box-shadow: 0 20px 40px rgba(56, 189, 248, 0.35);
        }
        .error {
            margin-top: 16px;
            padding: 10px 14px;
            border-radius: 10px;
            background: rgba(248, 113, 113, 0.12);
            color: #f87171;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class=\"card\">
        <h1>验证访问权限</h1>
        <p>请输入访问口令，以登录 Z.ai 2 API 仪表盘。</p>
        <form method=\"post\">
            <input type=\"password\" name=\"token\" placeholder=\"访问口令\" autocomplete=\"current-password\" required />
            <button type=\"submit\">登录仪表盘</button>
        </form>
        {% if error %}
        <div class=\"error\">{{ error }}</div>
        {% endif %}
    </div>
</body>
</html>
"""


MODEL_ID_ALIAS_SOURCE: Dict[str, str] = {
        "glm-4.5v": "GLM-4.5V",
        "0727-106B-API": "GLM-4.5-Air",
        "0727-360B-API": "GLM-4.5",
        "0808-360B-DR": "0808-360b-Dr",
        "deep-research": "Z1-Rumination",
        "GLM-4-6-API-V1": "GLM-4.6",
        "glm-4-flash": "GLM-4-Flash",
        "GLM-4.1V-Thinking-FlashX": "GLM-4.1V-Thinking-FlashX",
        "main_chat": "GLM-4-32B",
        "zero": "Z1-32B",
}

MODEL_ID_ALIASES: Dict[str, str] = {}
for key, value in MODEL_ID_ALIAS_SOURCE.items():
        MODEL_ID_ALIASES[key] = value
        MODEL_ID_ALIASES[key.lower()] = value


BASE_MODEL_VARIANT_DEFINITIONS: Dict[str, Dict[str, Any]] = {
        "GLM-4.5": {
                "upstream_id": "0727-360B-API",
                "description": "标准模型，通用对话，平衡性能",
                "thinking_description": "思考模型，显示推理过程，透明度高",
                "search_description": "搜索模型，实时网络搜索，信息更新",
        },
        "GLM-4.5V": {
                "upstream_id": "glm-4.5v",
                "description": "视觉模型，支持多模态理解",
        },
        "GLM-4.5-Air": {
                "upstream_id": "0727-106B-API",
                "description": "轻量模型，优先响应速度",
        },
        "0808-360b-Dr": {
                "upstream_id": "0808-360B-DR",
                "description": "深度研究模型，适合长文本",
        },
        "Z1-Rumination": {
                "upstream_id": "deep-research",
                "description": "Z1 深度推理模型",
                "default_features": {
                        "enable_thinking": True,
                        "web_search": True,
                        "auto_web_search": True,
                },
                "search_description": "Z1 深度推理模型（增强搜索）",
        },
        "GLM-4.6": {
                "upstream_id": "GLM-4-6-API-V1",
                "description": "GLM 4.6 标准模型",
        },
        "GLM-4-Flash": {
                "upstream_id": "glm-4-flash",
                "description": "Flash 模型，追求快速响应",
        },
        "GLM-4.1V-Thinking-FlashX": {
                "upstream_id": "GLM-4.1V-Thinking-FlashX",
                "description": "视觉 FlashX 模型",
                "default_features": {
                        "enable_thinking": True,
                },
        },
        "GLM-4-32B": {
                "upstream_id": "main_chat",
                "description": "32B 规格的通用模型",
        },
        "Z1-32B": {
                "upstream_id": "zero",
                "description": "Z1 32B 规格模型",
        },
}

DEFAULT_VARIANT_FEATURES: Dict[str, Any] = {
        "enable_thinking": False,
        "web_search": False,
        "auto_web_search": False,
}

DEFAULT_SEARCH_MCP_SERVERS = ["deep-web-search"]


def _build_model_variant_config(definitions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        config: Dict[str, Dict[str, Any]] = {}
        for alias, meta in definitions.items():
                upstream_id = meta.get("upstream_id")
                if not upstream_id:
                        continue

                base_features = dict(DEFAULT_VARIANT_FEATURES)
                base_features.update(meta.get("default_features", {}))

                base_mcp_servers = meta.get("mcp_servers")
                if base_mcp_servers is None:
                        base_mcp_servers = []

                base_entry = {
                        "upstream_id": upstream_id,
                        "description": meta.get("description") or f"{alias} 标准模型",
                        "features": base_features,
                        "mcp_servers": list(base_mcp_servers),
                }
                if base_entry["features"].get("web_search") and not base_entry["mcp_servers"]:
                        base_entry["mcp_servers"] = list(DEFAULT_SEARCH_MCP_SERVERS)
                config[alias] = base_entry

                thinking_features = dict(base_features)
                thinking_features["enable_thinking"] = True
                thinking_entry = {
                        "upstream_id": upstream_id,
                        "description": meta.get("thinking_description") or f"{alias} 思考模型",
                        "features": thinking_features,
                        "mcp_servers": list(base_entry["mcp_servers"]),
                }
                config[f"{alias}-Thinking"] = thinking_entry

                search_features = dict(base_features)
                search_features["web_search"] = True
                search_features["auto_web_search"] = True
                search_mcp_servers = set(base_entry["mcp_servers"] or [])
                if not search_mcp_servers:
                        search_mcp_servers = set(DEFAULT_SEARCH_MCP_SERVERS)
                config[f"{alias}-Search"] = {
                        "upstream_id": upstream_id,
                        "description": meta.get("search_description") or f"{alias} 搜索模型",
                        "features": search_features,
                        "mcp_servers": list(search_mcp_servers),
                }

        return config


MODEL_VARIANT_CONFIG = _build_model_variant_config(BASE_MODEL_VARIANT_DEFINITIONS)

for upstream_id, alias in MODEL_ID_ALIAS_SOURCE.items():
        cfg.model.mapping[upstream_id] = alias.lower()


# tiktoken 预加载
cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiktoken') + os.sep
os.environ["TIKTOKEN_CACHE_DIR"] = cache_dir
assert os.path.exists(os.path.join(cache_dir, "9b5ad71b2ce5302211f9c61530b329a4922fc6a4")) # cl100k_base.tiktoken
import tiktoken
enc = tiktoken.get_encoding("cl100k_base")
# 日志
logging.basicConfig(
        level=logging.DEBUG if cfg.api.debug_msg else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

DEBUG_MODE = cfg.api.debug_msg


def debug(message: str, *args: Any):
        if DEBUG_MODE:
                log.debug(message, *args)

# Flask 应用
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.secret_key = SECRET_KEY
app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")

phaseBak = "thinking"


@app.before_request
def _setup_request_context():
        g.current_token_info = {"token": None, "source": "none"}
# 工具函数
class utils:
        @staticmethod
        class request:
                _user_cache: Dict[str, Dict[str, Any]] = {}

                @classmethod
                def headers(cls) -> Dict[str, str]:
                        return {
                                "Accept": "*/*",
                                "Accept-Language": "zh-CN,zh;q=0.9",
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive",
                                "Origin": f"{cls.source.protocol}//{cls.source.host}",
                                "Pragma": "no-cache",
                                "Referer": f"{cls.source.protocol}//{cls.source.host}/",
                                "Sec-Ch-Ua": '"Microsoft Edge";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
                                "Sec-Ch-Ua-Mobile": "?0",
                                "Sec-Ch-Ua-Platform": '"Windows"',
                                "Sec-Fetch-Dest": "empty",
                                "Sec-Fetch-Mode": "cors",
                                "Sec-Fetch-Site": "same-origin",
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
                                "X-FE-Version": "prod-fe-1.0.98",
                        }

                @staticmethod
                def _last_user_message(messages: Any) -> str:
                        if not isinstance(messages, list):
                                return ""
                        for message in reversed(messages):
                                if not isinstance(message, dict):
                                        continue
                                if message.get("role") not in {"user", "human"}:
                                        continue
                                content = message.get("content")
                                if isinstance(content, str):
                                        return content
                                if isinstance(content, list):
                                        parts: List[str] = []
                                        for item in content:
                                                if not isinstance(item, dict):
                                                        continue
                                                if item.get("type") == "text" and item.get("text"):
                                                        parts.append(item.get("text", ""))
                                                elif item.get("type") == "input_text" and item.get("input_text"):
                                                        parts.append(item.get("input_text", ""))
                                        if parts:
                                                return "".join(parts)
                        return ""

                @staticmethod
                def chat(data, chat_id):
                        timestamp = int(datetime.now().timestamp() * 1000)
                        request_id = str(uuid.uuid4())

                        user = utils.request.user()
                        user_token = user.get("token")
                        user_id = user.get("id")

                        params = {
                                "timestamp": timestamp,
                                "requestId": request_id,
                        }
                        headers = {
                                **cfg.headers(),
                                "Content-Type": "application/json",
                                "Referer": f"{BASE_URL}/c/{chat_id}"
                        }
                        if user_token:
                                headers["Authorization"] = f"Bearer {user_token}"

                        if user_id:
                                params["user_id"] = user_id
                                last_user_message = utils.request._last_user_message(data.get("messages", []))
                                try:
                                        signatures = utils.request.signature(
                                                {
                                                        "requestId": request_id,
                                                        "timestamp": timestamp,
                                                        "user_id": user_id,
                                                },
                                                last_user_message,
                                        )
                                        headers["X-Signature"] = signatures.get("signature")
                                        params["signature_timestamp"] = signatures.get("timestamp")
                                        data["signature_prompt"] = last_user_message
                                except Exception as exc:
                                        debug("签名生成失败: %s", exc)

                        url = f"{BASE_URL}/api/chat/completions"
                        if params:
                                query_string = urllib.parse.urlencode(params)
                                url = f"{url}?{query_string}"

                        token_info = dict(getattr(g, "current_token_info", {}) or {})
                        start_time = time.perf_counter()
                        recorded = False
                        try:
                                response = requests.post(
                                        url,
                                        json=data,
                                        headers=headers,
                                        stream=True,
                                        timeout=60,
                                )
                                duration = time.perf_counter() - start_time
                                _record_upstream_metrics(
                                        method="POST",
                                        url=url,
                                        status_code=response.status_code,
                                        duration=duration,
                                        token_info=token_info,
                                )
                                recorded = True
                                response._metrics_context = {
                                        "start_time": start_time,
                                        "url": url,
                                        "method": "POST",
                                        "token": token_info.get("token"),
                                        "token_info": token_info,
                                        "finalized": False,
                                }
                                return response
                        except Exception as exc:
                                if not recorded:
                                        duration = time.perf_counter() - start_time
                                        _record_upstream_metrics(
                                                method="POST",
                                                url=url,
                                                status_code=None,
                                                duration=duration,
                                                error=str(exc),
                                                token_info=token_info,
                                        )
                                if token_pool.contains(token_info.get("token")):
                                        token_pool.mark_failure(token_info.get("token"))
                                raise

                @staticmethod
                def image(data_url, chat_id):
                        if cfg.api.anon or not isinstance(data_url, str) or not data_url.startswith("data:"):
                                return None

                        header, encoded = data_url.split(",", 1)
                        mime_type = header.split(";")[0].split(":")[1] if ":" in header else "image/jpeg"

                        image_data = base64.b64decode(encoded)
                        extension = mimetypes.guess_extension(mime_type) or ""
                        if extension == ".jpe":
                                extension = ".jpg"
                        if not extension and mime_type.startswith("image/"):
                                extension = f".{mime_type.split('/', 1)[1]}"
                        filename = f"{uuid.uuid4()}{extension}"

                        debug("上传文件：%s", filename)
                        token = utils.request.token(prefer_pool=True)
                        headers = {
                                **cfg.headers(),
                                "Referer": f"{BASE_URL}/c/{chat_id}"
                        }
                        if token:
                                headers["Authorization"] = f"Bearer {token}"

                        url = f"{BASE_URL}/api/v1/files/"
                        start_time = time.perf_counter()
                        recorded = False
                        try:
                                response = requests.post(
                                        url,
                                        files={"file": (filename, image_data, mime_type)},
                                        headers=headers,
                                        timeout=30,
                                )
                                duration = time.perf_counter() - start_time
                                _record_upstream_metrics(
                                        method="POST",
                                        url=url,
                                        status_code=response.status_code,
                                        duration=duration,
                                        token_info=dict(getattr(g, "current_token_info", {}) or {}),
                                )
                                recorded = True

                                if token_pool.contains(token):
                                        if response.status_code in (401, 403):
                                                token_pool.mark_failure(token)
                                        else:
                                                token_pool.mark_success(token)

                                if response.status_code == 200:
                                        result = response.json()
                                        return f"{result.get('id')}_{result.get('filename')}"
                                debug("图片上传失败: %s", response.text)
                        except Exception as exc:
                                if not recorded:
                                        duration = time.perf_counter() - start_time
                                        _record_upstream_metrics(
                                                method="POST",
                                                url=url,
                                                status_code=None,
                                                duration=duration,
                                                error=str(exc),
                                                token_info=dict(getattr(g, "current_token_info", {}) or {}),
                                        )
                                debug("图片上传失败: %s", exc)
                        return None

                @staticmethod
                def id(prefix = "msg") -> str:
                        return str(uuid.uuid4())

                @staticmethod
                def token(prefer_pool: bool = True) -> str:
                        token: Optional[str] = None
                        source = "none"
                        g.current_token_info = {"token": None, "source": source}

                        if prefer_pool:
                                token = token_pool.get()
                                if token:
                                        source = "pool"
                                        g.current_token_info = {"token": token, "source": source}
                                        debug("使用池中令牌: %s...", token[:10])
                                        return token

                        if not cfg.api.anon and cfg.source.token:
                                source = "static"
                                g.current_token_info = {"token": cfg.source.token, "source": source}
                                return cfg.source.token

                        try:
                                headers = {**cfg.headers(), "Content-Type": "application/json"}
                                response = requests.get(f"{BASE_URL}/api/v1/auths/", headers=headers, timeout=8)
                                data = response.json()
                                token = data.get("token")
                                if token:
                                        source = "anonymous"
                                        g.current_token_info = {"token": token, "source": source}
                                        g.current_user_info = {
                                                "token": token,
                                                "id": data.get("id"),
                                                "name": data.get("name"),
                                        }
                                        utils.request._user_cache[token] = {
                                                "id": data.get("id"),
                                                "name": data.get("name"),
                                        }
                                        debug("获取匿名令牌: %s...", token[:15])
                                        return token
                        except Exception as exc:
                                debug("匿名令牌获取失败: %s", exc)

                        fallback_token = cfg.source.token or token
                        if fallback_token:
                                source = "static" if fallback_token == cfg.source.token else "anonymous"
                        else:
                                source = "anonymous"
                        g.current_token_info = {"token": fallback_token, "source": source}
                        return fallback_token or ""

                @staticmethod
                def user(prefer_pool: bool = True):
                        existing = getattr(g, "current_user_info", None)
                        if existing and existing.get("token"):
                                return existing

                        token = utils.request.token(prefer_pool=prefer_pool)
                        info = getattr(g, "current_user_info", None)
                        if info and info.get("token") == token:
                                return info

                        if not token:
                                info = {"token": "", "id": None, "name": None}
                                g.current_user_info = info
                                return info

                        cached = utils.request._user_cache.get(token)
                        if cached:
                                info = {"token": token, "id": cached.get("id"), "name": cached.get("name")}
                                g.current_user_info = info
                                return info

                        headers = {**cfg.headers(), "Content-Type": "application/json"}
                        if token:
                                headers["Authorization"] = f"Bearer {token}"

                        try:
                                response = requests.get(f"{BASE_URL}/api/v1/auths/", headers=headers, timeout=8)
                                if response.status_code == 200:
                                        data = response.json()
                                        info = {
                                                "token": token,
                                                "id": data.get("id"),
                                                "name": data.get("name"),
                                        }
                                        utils.request._user_cache[token] = {
                                                "id": data.get("id"),
                                                "name": data.get("name"),
                                        }
                                        g.current_user_info = info
                                        return info
                        except Exception as exc:
                                debug("获取用户信息失败: %s", exc)

                        info = {"token": token, "id": None, "name": None}
                        g.current_user_info = info
                        return info

                @staticmethod
                def signature(prarms: Dict, content: str) -> Dict:
                        for param in ["timestamp", "requestId", "user_id"]:
                                if param not in prarms or not prarms.get(param):
                                        raise ValueError(f"need prarm: {param}")

                        def _hmac_sha256(key: bytes, msg: bytes):
                                return hmac.new(key, msg, hashlib.sha256).hexdigest()

                        # content = content.strip()
                        request_time = int(prarms.get("timestamp", datetime.now().timestamp() * 1000))  # 请求时间戳（毫秒）

                        # 第 1 级签名
                        signature_expire = request_time // (5 * 60 * 1000)  # 5 分钟粒度
                        signature_1_plaintext = str(signature_expire)
                        signature_1 = _hmac_sha256(b"junjie", signature_1_plaintext.encode('utf-8'))

                        # 第 2 级签名
                        content = base64.b64encode(content.encode('utf-8')).decode('ascii')

                        signature_prarms = str(','.join([f"{k},{prarms[k]}" for k in sorted(prarms.keys())]))
                        signature_2_plaintext = f"{signature_prarms}|{content}|{str(request_time)}"
                        signature_2 = _hmac_sha256(signature_1.encode('utf-8'), signature_2_plaintext.encode('utf-8'))

                        # .......:.---*==**==+===-=-::.....   .::::.:.................:::.:-::-:-::.
                        # .....:.::==:+--==--=---:-=:::..  .-=+++*+++++=-:.   ......:.::::.:-:----:.
                        # ...:.....::--::-----::::.::::. .-+*************++-:. .::..:.:::-::----=-::.
                        # ........:..:.:-=-----::::::...:+++************+++++-. ..:::.:::---:=====-..
                        # .......:.::::=-=-----:::::::..=*++++**********+++++=:  .-::::::--=--++++=..      .
                        # ......::..:::=-=-----::-*-=:.:=*+++++==+++=+====+===:..::.::--::--==++++=:.    ..
                        # ........:-:::::---::::-:=::: -+=+++==++=++======++==-...===:.:-----=+*+==:..
                        # . ..:++++*--=-=##+*=::-::::--=************++*+++*+==-.:.-:-:.:::---=+%%%%=..      .
                        #   ...:-=:.::::-%#=-:...::.:=++****#*******++**#***++-:-:-+-:==:..:--*%@@@*.... .:--
                        # ..::::::::--:::=---::::::..+*+*******+===::-+*****+=::---=--::.:::----=++=-::::.-==
                        # ::::::::::::---:-:::-:--:---*++***+*****+==+++++++==::=-------=--------------------
                        # :::::::::---------------: .:+++***++=++*+*+=-=++++=--==============================
                        # :::::--------------------::--=+****: .:...:. -+*++-================================
                        # ::::--------------------------==***+-+*+++-:-+*+=-=++=++++==++===============++====
                        # ----------------------------===-=*#**++**+++***--..-=++++++++++++++++++++++++++++++
                        # --------------------============--+**********+-:-.   :+++++++++++++++++++++++++++=+
                        # ------------------=============-. .-=++++++=-::--:    .=+*++++++++++++++++++======+
                        # --------====------===========-:...::..:-:::::::---   ...:=+++++++++++++++++++++++==
                        # -------=================---:......-=::=+==+=-::--:........-=+**++++++++++++++++++++
                        # --------==--========--::......... :++++*++**=:::::=-........:-=++++++++++++++++++++
                        # -----=---====----::.............:--+********+=-:==:.............:::-=++++++++--++==
                        # ======.:.:=---=-........ .....:=+**+**###*****++=:...................-+*++**+*+++++
                        # ======:.-----==:....... ......-*******##********++=....................-++++++:-+++
                        # +++++*+++*+++-................=*******************+:....................=*+*+++++++
                        # +++++++++++*-................-+********************-................... -*+++++++++
                        # +++++++++++*-...............:=+********************-..............::::..=++++++++++
                        # ++++++++++++=...............:=+********************-...............:---:-++++++++++
                        # +++++++++++*= ............   .-*******************=..:...................++++++++++
                        # +++++++**++=:........  .....  -*++**************+=: .....................-+++++++++
                        # +++++++=-:....................=*+=+++++=++******=:   ....--...............-++++++++
                        # ++++++-......................:***+==---::-==+===-........::.. .............=+++++++
                        # ++++++........................***+=-::.. .:-----:.:-..... .................:+======
                        # +++++=........................-==-:..     ...::...:-........................-++++++
                        # ++++++..........................          .      ............................:+++++
                        # ++++++.........................        .....        .........................:+++++
                        # ++++++: ........................  ...........    ............................-*++++
                        # +++++*- ........................  ............   ............ ...............=+++++
                        # 哎呀！哎呀！哎呀呀呀！
                        # 哎↘呀哎↘↗呀哎呀呀呀
                        # junjie，jun 总啊！
                        # 您怎么就改了签名算法啊哎呀！
                        # 哎呀哎呀哎呀呀呀呀呀
                        # 太感谢我 jun 总了呀呀呀呀
                        # 太性情 太感谢 太通透了
                        # 直接就宣判了啊！
                        # 这可是带 hmac 的签名算法
                        # 砸到小户身上脸都是疼的~
                        # 祝开发此签名的开发者
                        # 学业工作都顺利
                        # 用苹果手机
                        # 开苹果汽车
                        # 住苹果房子
                        # 享苹果人生
                        # 你必定是
                        # 开兰博基尼
                        # 坐私人飞机
                        # 同时也祝您和您的家里人
                        # 身体健康
                        # 事业顺利
                        # 家庭幸福
                        # 在以后的人生里
                        # 购买力越来越苹果爆赞👍

                        log.debug("生成签名: %s", signature_2)
                        log.debug("  请求时间: %s", prarms.get("timestamp"))
                        log.debug("  请求标识: %s", prarms.get("requestId"))
                        log.debug("  用户标识: %s", prarms.get("user_id"))
                        log.debug("  最后内容: %s", content[:50])
                        return {
                                "signature": signature_2,
                                "timestamp": request_time
                        }

                _models_cache = {}
                @staticmethod
                def models() -> Dict:
                        """获取模型列表"""
                        current_token: Optional[str]
                        if cfg.api.anon:
                                if has_request_context():
                                        current_token = utils.request.user().get('token')
                                else:
                                        log.debug("跳过请求上下文外的用户令牌解析，回退到静态令牌")
                                        current_token = cfg.source.token
                        else:
                                current_token = cfg.source.token

                        if not current_token:
                                cached = utils.request._models_cache
                                if cached:
                                        return cached
                                log.debug("无法获取模型列表：缺少有效令牌")
                                return {"object": "list", "data": []}

                        if utils.request._models_cache:
                                return utils.request._models_cache

                        def format_model_name(name: str) -> str:
                                """格式化模型名"""
                                if not name:
                                        return ""
                                parts = name.split('-')
                                if len(parts) == 1:
                                        return parts[0].upper()
                                formatted = [parts[0].upper()]
                                for p in parts[1:]:
                                        if not p:
                                                formatted.append("")
                                        elif p.isdigit():
                                                formatted.append(p)
                                        elif any(c.isalpha() for c in p):
                                                formatted.append(p.capitalize())
                                        else:
                                                formatted.append(p)
                                return "-".join(formatted)

                        def get_model_name(source_id: str, model_name: str) -> str:
                                """获取模型名称：优先自带，其次智能生成"""

                                # 处理自带系列名的模型名称
                                if source_id.startswith(("GLM", "Z")) and "." in source_id:
                                        return source_id

                                if model_name.startswith(("GLM", "Z")) and "." in model_name:
                                        return model_name

                                # 无法识别系列名，但名称仍为英文
                                if not model_name or not ('A' <= model_name[0] <= 'Z' or 'a' <= model_name[0] <= 'z'):
                                        model_name = format_model_name(source_id)
                                        if not model_name.upper().startswith(("GLM", "Z")): model_name = model_name = "GLM-" + format_model_name(source_id)

                                return model_name

                        def get_model_id(source_id: str, model_name: str) -> str:
                                """获取模型 ID：优先配置，其次智能生成"""
                                if hasattr(cfg.model, 'mapping') and source_id in cfg.model.mapping:
                                        return cfg.model.mapping[source_id]

                                # 找不到配置则生成智能 ID
                                smart_id = model_name.lower()
                                cfg.model.mapping[source_id] = smart_id
                                return smart_id

                        headers = {
                                **cfg.headers(),
                                "Authorization": f"Bearer {current_token}",
                                "Content-Type": "application/json"
                        }
                        response = requests.get(f"{cfg.source.protocol}//{cfg.source.host}/api/models", headers=headers)
                        if response.status_code == 200:
                                data = response.json()
                                models = []
                                for m in data.get("data", []):
                                        if not m.get("info", {}).get("is_active", True):
                                                continue
                                        model_id = m.get("id")
                                        model_name = m.get("name")
                                        model_info = m.get("info", {})
                                        model_meta = model_info.get("meta", {})
                                        model_logo = "data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2030%2030%22%20style%3D%22background%3A%232D2D2D%22%3E%3Cpath%20fill%3D%22%23FFFFFF%22%20d%3D%22M15.47%207.1l-1.3%201.85c-.2.29-.54.47-.9.47h-7.1V7.09c0%20.01%209.31.01%209.31.01z%22%2F%3E%3Cpath%20fill%3D%22%23FFFFFF%22%20d%3D%22M24.3%207.1L13.14%2022.91H5.7l11.16-15.81z%22%2F%3E%3Cpath%20fill%3D%22%23FFFFFF%22%20d%3D%22M14.53%2022.91l1.31-1.86c.2-.29.54-.47.9-.47h7.09v2.33h-9.3z%22%2F%3E%3C%2Fsvg%3E"

                                        model_meta_r = {
                                                "profile_image_url": model_logo,
                                                "capabilities": model_meta.get("capabilities"),
                                                "description": model_meta.get("description"),
                                                "hidden": model_meta.get("hidden"),
                                                "suggestion_prompts": [{"content": item["prompt"]} for item in (model_meta.get("suggestion_prompts") or []) if isinstance(item, dict) and "prompt" in item]
                                        }
                                        models.append({
                                                "id": get_model_id(model_id, get_model_name(model_id, model_name)),
                                                "object": "model",
                                                "name": get_model_name(model_id, model_name),
                                                "meta": model_meta_r,
                                                "info": {
                                                        "meta": model_meta_r
                                                },
                                                "created": model_info.get("created_at", int(datetime.now().timestamp())),
                                                "owned_by": "z.ai",
                                                "orignal": {
                                                        "name": model_name,
                                                        "id": model_id,
                                                        "info": model_info
                                                },
                                                # Special For Open WebUI
                                                # So, Fuck you! Private!
                                                "access_control": None,
                                        })
                                result = {
                                        "object": "list",
                                        "data": models,
                                }
                                utils.request._models_cache = result
                                return result
                        else:
                                raise Exception(f"fetch models info fail: {response.text}")

        @staticmethod
        def response(resp):
                        try:
                                origin = request.headers.get("Origin", "").strip()
                        except Exception:
                                origin = ""

                        allow_origin = origin or request.host_url.rstrip("/")

                        existing_vary = resp.headers.get("Vary")
                        vary_values = set(
                                value.strip()
                                for value in (existing_vary or "").split(",")
                                if value.strip()
                        )
                        vary_values.add("Origin")

                        resp.headers.update({
                                "Access-Control-Allow-Origin": allow_origin,
                                "Access-Control-Allow-Methods": "GET, POST, OPTIONS, DELETE",
                                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Auth-Token",
                                "Access-Control-Allow-Credentials": "true",
                                "Vary": ", ".join(sorted(vary_values)),
                        })
                        return resp

        @staticmethod
        def format(data: Dict, type: str = "OpenAI"):
            odata = {**data.copy()}
            new_messages = []
            chat_id = odata.get("chat_id")
            model = odata.get("model", cfg.model.default)

            models = utils.request.models() # 请求模型信息，以获取映射设置
            # 如果找到了映射设置
            if hasattr(cfg.model, 'mapping') and model:
                # 在映射中查找值等于当前模型的键
                for source_id, mapped_id in cfg.model.mapping.items():
                    if mapped_id == model and model != source_id:
                        # 找到匹配，将 model 改为源 ID（键名）
                        log.debug(f"模型映射: {model} -> {source_id}")
                        model = source_id
                        break

            # Anthropic - system 转换 role:system
            if "system" in odata:
                systems = odata["system"]
                if isinstance(systems, str):
                    content = systems.lstrip('\n')
                else:
                    items = []
                    for item in systems:
                        if item.get("type") == "text": items.append(item.get("text", "").lstrip('\n'))
                    content = "\n\n".join(items)
                new_messages.append({"role": "system", "content": content})
                del odata["system"]

            # messages 处理
            for message in odata.get("messages", []):
                role = message.get("role")
                content = message.get("content", [])
                new_message = {"role": role}

                # 如果 content 类型是文本
                if isinstance(content, str):
                    new_message["content"] = content
                    new_messages.append(new_message)
                    continue

                # 如果 content 类型是数组
                if isinstance(content, list):
                    dont_append = False
                    new_content: Union[str, List[Dict[Any, Any]]] = ""
                    for item in content:
                        type = item.get("type")
                        # 如果 消息类型 为 文本
                        if type == "text":
                            new_content = item.get("text")
                            continue

                        # 如果 消息类型 为 图片
                        elif type == "image_url" or type == "image":
                            media_url = ""
                            # 获取 OpenAI 格式下的图片链接
                            if item.get("image_url", {}).get("url"):
                                media_url = item.get("image_url").get("url")
                            # 获取 Anthropic 格式下的图片链接
                            elif item.get("source", {}).get("data"):
                                source = item.get("source")
                                if source.get("type") == "base64" and source.get("data"):
                                    media_url = f"data:{source.get("media_type", "image/jpeg")};base64,{source.get("data")}"

                            def truncate_values(obj, max_len=20):
                                if isinstance(obj, dict): return {k: truncate_values(v, max_len) for k, v in obj.items()}
                                elif isinstance(obj, list): return [truncate_values(x, max_len) for x in obj]
                                elif isinstance(obj, str): return obj[:max_len]
                                else: return obj

                            if not media_url:
                                if isinstance(new_content, str):
                                    new_content = [{
                                        "type": "text",
                                        "text": new_content
                                    }]
                                new_content.append({
                                    "type": "text",
                                    "text": f"system: image error - Unsupported format or missing URL\norignal data:{json.dumps(truncate_values(item), ensure_ascii=False)}"
                                })
                                continue
                            # 将以 data: 编码的图片链接上传到服务器
                            try:
                                uploaded_url = utils.request.image(media_url, chat_id)
                                if uploaded_url: media_url = uploaded_url
                            except Exception as e:
                                if isinstance(new_content, str):
                                    new_content = [{
                                        "type": "text",
                                        "text": new_content
                                    }]
                                new_content.append({
                                    "type": "text",
                                    "text": f"system: image upload error - {e}\norignal data:{json.dumps(truncate_values(item), ensure_ascii=False)}"
                                })
                                continue

                            if isinstance(new_content, str):
                                new_content = [{
                                    "type": "text",
                                    "text": new_content
                                }]
                            new_content.append({
                                "type": "image_url",
                                "image_url": {"url": media_url}
                            })

                        # Anthropic - 如果 消息类型 为 助理 使用工具
                        elif type == "tool_use" and role == "assistant":
                            # 如果 tool_calls 为空，初始化为空列表
                            if new_message.get("tool_calls") is None:
                                new_message["tool_calls"] = []

                            # 直接追加到 new_msg["tool_calls"]
                            new_message["tool_calls"].append({
                                "id": item.get("id"),
                                "type": "function",
                                "function": {
                                    "name": item.get("name"),
                                    "arguments": json.dumps(item.get("input", {}) or {}, ensure_ascii=False)
                                }
                            })
                            dont_append = True

                        # Anthropic - 如果 消息类型 为 工具结果
                        elif type == "tool_result":
                            tool_result_content = item.get("content", [])

                            # 如果 工具请求结果 类型是数组
                            if isinstance(tool_result_content, list):
                                # 提取所有 text 类型的内容并拼接
                                _parts = []
                                for _item in tool_result_content:
                                    if _item.get("type") == "text" and _item.get("text", ""): _parts.append(_item.get("text"))
                                if _parts:
                                    result = "".join(_parts)
                            else:
                                result = tool_result_content

                            new_messages.append({
                                "role": "tool",
                                "tool_call_id": item.get("tool_use_id"),
                                "content": result
                            })
                            dont_append = True

                        # 如果 消息类型 为 其它
                        else:
                            if isinstance(new_content, str):
                                new_content = [{
                                    "type": "text",
                                    "text": new_content
                                }]
                            new_content.append(item)

                    if not dont_append:
                        new_message["content"] = new_content
                        new_messages.append(new_message)

            result = {
                **odata,
                "model": model,
                "messages": new_messages,
                "stream": True,
                "features": {
                    "enable_thinking": False, # 默认思考
                    **odata.get("features", {})
                },
            }

            # Qwen 的开启思考方式
            if odata.get("enable_thinking"):
                result["features"]["enable_thinking"] = str(odata.get("enable_thinking", True))
                odata.pop("enable_thinking", None)

            # Anthropic / CherryStudio-OpenAI 的开启思考方式
            if odata.get("thinking"):
                result["features"]["enable_thinking"] = str(odata.get("thinking", {}).get("type", "enabled")).lower() == "enabled"
                odata.pop("thinking", None)

            if models:
                for _model in models.get("data", []):
                    if _model.get("id") == model or _model.get("orignal", {}).get("id") == model:
                        # 检查该模型是否支持 thinking 能力
                        if not _model.get("orignal", {}).get("info", {}).get("meta", {}).get("capabilities", {}).get("think", False):
                            del result["features"]["enable_thinking"]
                            # 如果 features 为空，删除整个 features 字段
                            if not result["features"]:
                                del result["features"]
                        break


            return result

class response:
        @staticmethod
        def parse(stream):
            for line in stream.iter_lines():
                if not line or not line.startswith(b"data: "): continue
                try: data = json.loads(line[6:].decode("utf-8", "ignore"))
                except: continue
                yield data

        @staticmethod
        def format(data, type = "OpenAI"):
            data = data.get("data", "")
            if not data: return None
            phase = data.get("phase", "other")
            content = data.get("delta_content") or data.get("edit_content") or ""
            if not content: return None
            contentBak = content
            global phaseBak

            if phase == "tool_call":
                content = re.sub(r"\n*<glm_block[^>]*>{\"type\": \"mcp\", \"data\": {\"metadata\": {", "{", content)
                content = re.sub(r"\", \"result\": \"\".*</glm_block>", "", content)
            elif phase == "other" and phaseBak == "tool_call" and "glm_block" in content:
                phase = "tool_call"
                content = re.sub(r"null, \"display_result\": \"\".*</glm_block>", "\"}", content)

            if phase == "thinking" or (phase == "answer" and "summary>" in content):
                content = re.sub(r"(?s)<details[^>]*?>.*?</details>", "", content)
                content = content.replace("</thinking>", "").replace("<Full>", "").replace("</Full>", "")

                if phase == "thinking":
                    content = re.sub(r'\n*<summary>.*?</summary>\n*', '\n\n', content)

                # 以 <reasoning> 为基底
                content = re.sub(r"<details[^>]*>\n*", "<reasoning>\n\n", content)
                content = re.sub(r"\n*</details>", "\n\n</reasoning>", content)

                if phase == "answer":
                    match = re.search(r"(?s)^(.*?</reasoning>)(.*)$", content) # 判断 </reasoning> 后是否有内容
                    if match:
                        before, after = match.groups()
                        if after.strip():
                            # </reasoning> 后有内容
                            if phaseBak == "thinking":
                                # 思考休止 → 结束思考，加上回答
                                content = f"\n\n</reasoning>\n\n{after.lstrip('\n')}"
                            elif phaseBak == "answer":
                                # 回答休止 → 清除所有
                                content = ""
                        else:
                            # 思考休止 → </reasoning> 后无内容
                            content = "\n\n</reasoning>"

                if cfg.api.think == "reasoning":
                    if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                    content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                    content = re.sub(r"<reasoning>\n*", "", content)
                    content = re.sub(r"\n*</reasoning>", "", content)
                elif cfg.api.think == "think":
                    if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                    content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                    content = re.sub(r"<reasoning>", "<think>", content)
                    content = re.sub(r"</reasoning>", "</think>", content)
                elif cfg.api.think == "strip":
                    content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                    content = re.sub(r"<reasoning>\n*", "", content)
                    content = re.sub(r"</reasoning>", "", content)
                elif cfg.api.think == "details":
                    if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                    content = re.sub(r"<reasoning>", "<details type=\"reasoning\" open><div>", content)
                    thoughts = ""
                    if phase == "answer":
                        # 判断是否有 <summary> 内容
                        summary_match = re.search(r"(?s)<summary>.*?</summary>", before)
                        duration_match = re.search(r'duration="(\d+)"', before)
                        if summary_match:
                            # 有内容 → 直接照搬
                            thoughts = f"\n\n{summary_match.group()}"
                        # 判断是否有 duration 内容
                        elif duration_match:
                            # 有内容 → 通过 duration 生成 <summary>
                            thoughts = f'\n\n<summary>Thought for {duration_match.group(1)} seconds</summary>'
                    content = re.sub(r"</reasoning>", f"</div>{thoughts}</details>", content)
                else:
                    content = re.sub(r"</reasoning>", "</reasoning>\n\n", content)
                    log.warning("警告: THINK_TAGS_MODE 传入了未知的替换模式，将使用 <reasoning> 标签。")

            phaseBak = phase
            if repr(content) != repr(contentBak):
                log.debug("R 内容: %s %s", phase, repr(contentBak))
                log.debug("W 内容: %s %s", phase, repr(content))
            else:
                log.debug("R 内容: %s %s", phase, repr(contentBak))

            if phase == "thinking" and cfg.api.think == "reasoning":
                if type == "Anthropic": return {"type": "thinking_delta", "thinking": content}
                return {"role": "assistant", "reasoning_content": content}
            if phase == "tool_call":
                return {"tool_call": content}
            elif repr(content):
                if type == "Anthropic": return {"type": "text_delta", "text": content}
                else: return {"role": "assistant", "content": content}
            else:
                return None

        @staticmethod
        def count(text):
            return len(enc.encode(text))

@app.route("/v1/models", methods=["GET", "POST", "OPTIONS"])
def models():
        if request.method == "OPTIONS":
                return utils.request.response(make_response())
        auth_error = _require_api_auth()
        if auth_error:
                return auth_error
        try:
                data = utils.request.models()
                models_list = list(data.get("data", []))

                existing_ids = {item.get("id") for item in models_list}
                upstream_created_map = {}
                for item in models_list:
                        upstream = item.get("orignal", {}) if isinstance(item.get("orignal"), dict) else {}
                        upstream_id = upstream.get("id")
                        if upstream_id:
                                upstream_created_map[upstream_id] = item.get("created", int(datetime.now().timestamp()))

                for variant_name, config in MODEL_VARIANT_CONFIG.items():
                        upstream_id = config.get("upstream_id")
                        if upstream_id and upstream_id not in upstream_created_map:
                                continue
                        if variant_name in existing_ids:
                                continue

                        metadata = {
                                "upstream_id": upstream_id,
                                "features": config.get("features", {}),
                                "mcp_servers": config.get("mcp_servers", []),
                        }
                        entry = {
                                "id": variant_name,
                                "object": "model",
                                "name": variant_name,
                                "created": upstream_created_map.get(upstream_id, int(datetime.now().timestamp())),
                                "owned_by": "z.ai",
                                "description": config.get("description", ""),
                                "metadata": metadata,
                        }
                        models_list.append(entry)
                        existing_ids.add(variant_name)

                return utils.request.response(jsonify({"object": "list", "data": models_list}))
        except Exception as e:
                log.error(traceback.format_exc())
                return utils.request.response(jsonify({
                        "error": 500,
                        "message": "错误: " + str(e)
        })), 500

@app.route("/v1/chat/completions", methods=["GET", "POST", "OPTIONS"])
def OpenAI_Compatible():
        response = None
        try:
                if request.method == "OPTIONS":
                        return utils.request.response(make_response())

                auth_error = _require_api_auth()
                if auth_error:
                        return auth_error

                odata = request.get_json(force=True, silent=True) or {}
                # log.debug("收到请求:")
                # log.debug("  data: %s", json.dumps(odata))
                id = utils.request.id("chat")
                stream = odata.get("stream", False)
                include_usage = odata.get("stream_options", {}).get("include_usage", True)

                data = {
                        **utils.request.format(odata, "OpenAI"),
                        "chat_id": id,
                        "id": utils.request.id(),
                }
                requested_model = (odata.get("model") or cfg.model.default or "").strip() or cfg.model.default
                variant_config = MODEL_VARIANT_CONFIG.get(requested_model)
                if variant_config:
                        upstream_model = variant_config.get("upstream_id") or requested_model
                        data["model"] = upstream_model
                        features = data.setdefault("features", {})
                        for key, value in variant_config.get("features", {}).items():
                                features[key] = value
                        existing_servers: List[str] = []
                        if isinstance(odata.get("mcp_servers"), list):
                                existing_servers = [str(server) for server in odata.get("mcp_servers") if isinstance(server, str)]
                        for server in variant_config.get("mcp_servers", []):
                                if server not in existing_servers:
                                        existing_servers.append(server)
                        if existing_servers:
                                data["mcp_servers"] = existing_servers
                        response_model = requested_model
                else:
                        response_model = data.get("model", requested_model)

                model = response_model
                messages = data.get("messages", [])

                # 仅当需要 usage 时才计算 prompt_tokens
                prompt_tokens: int = 0
                if include_usage:
                    prompt_tokens = utils.response.count("".join(
                        c if isinstance(c, str) else (c.get("text", "") if isinstance(c, dict) and c.get("type") == "text" else "")
                        for m in messages
                        for c in ([m["content"]] if isinstance(m.get("content"), str) else (m.get("content") or []))
                    ))
        
                response = utils.request.chat(data, id)
                if response.status_code != 200:
                        _finalize_upstream_response(response, error=response.text or f"HTTP {response.status_code}")
                        return utils.request.response(jsonify({
                                "error": response.status_code,
                                "message": response.text or None
                        })), response.status_code

                if stream:
                        def generate_stream():
                                completion_parts = []  # 收集 content 和 reasoning_content
                                error_message: Optional[str] = None
                                try:
                                        for raw_chunk in utils.response.parse(response):
                                                delta = utils.response.format(raw_chunk, "OpenAI")
                                                if not delta:
                                                        continue

                                                # 累积内容（用于后续 token 计算，仅当 include_usage=True）
                                                if include_usage:
                                                        if "content" in delta:
                                                                completion_parts.append(delta["content"])
                                                        if "reasoning_content" in delta:
                                                                completion_parts.append(delta["reasoning_content"])

                                                # 构造 SSE 响应
                                                yield f"data: {json.dumps({
                                                        "id": utils.request.id('chatcmpl'),
                                                        "object": "chat.completion.chunk",
                                                        "created": int(datetime.now().timestamp() * 1000),
                                                        "model": model,
                                                        "choices": [{
                                                                "index": 0,
                                                                "delta": delta,
                                                                "message": delta,
                                                                "finish_reason": None
                                                        }]
                                                })}\n\n"

                                        yield f"data: {json.dumps({
                                                'id': utils.request.id('chatcmpl'),
                                                'object': 'chat.completion.chunk',
                                                'created': int(datetime.now().timestamp() * 1000),
                                                'model': model,
                                                'choices': [{
                                                        'index': 0,
                                                        'delta': {"role": "assistant"},
                                                        'message': {"role": "assistant"},
                                                        'finish_reason': "stop"
                                                }]
                                        })}\n\n"

                                        # 发送 usage
                                        if include_usage:
                                                completion_str = "".join(completion_parts)
                                                completion_tokens = utils.response.count(completion_str)
                                                yield f"data: {json.dumps({
                                                        'id': utils.request.id('chatcmpl'),
                                                        'object': 'chat.completion.chunk',
                                                        'created': int(datetime.now().timestamp() * 1000),
                                                        'model': model,
                                                        'choices': [],
                                                        'usage': {
                                                                'prompt_tokens': prompt_tokens,
                                                                'completion_tokens': completion_tokens,
                                                                'total_tokens': prompt_tokens + completion_tokens
                                                        }
                                                })}\n\n"

                                        yield "data: [DONE]\n\n"
                                except GeneratorExit:
                                        error_message = "client disconnected"
                                        raise
                                except Exception as exc:
                                        error_message = str(exc)
                                        raise
                                finally:
                                        _finalize_upstream_response(response, error=error_message)

                        return Response(generate_stream(), mimetype="text/event-stream")

                else:
                        # 伪 - 非流式
                        content_parts = []
                        reasoning_parts = []

                        for raw_chunk in utils.response.parse(response):
                                if raw_chunk.get("data", {}).get("done"):
                                        break
                                delta = utils.response.format(raw_chunk)
                                if not delta:
                                        continue
                                if "content" in delta:
                                        content_parts.append(delta["content"])
                                if "reasoning_content" in delta:
                                        reasoning_parts.append(delta["reasoning_content"])

                        final_message = {"role": "assistant"}
                        completion_str = ""
                        if reasoning_parts:
                                reasoning_text = "".join(reasoning_parts)
                                final_message["reasoning_content"] = reasoning_text
                                completion_str += reasoning_text
                        if content_parts:
                                content_text = "".join(content_parts)
                                final_message["content"] = content_text
                                completion_str += content_text

                        completion_tokens = utils.response.count(completion_str)

                        result = {
                                "id": utils.request.id("chatcmpl"),
                                "object": "chat.completion",
                                "created": int(datetime.now().timestamp() * 1000),
                                "model": model,
                                "choices": [{
                                        "index": 0,
                                        "message": final_message,
                                        "finish_reason": "stop"
                                }]
                        }

                        if include_usage:
                                result["usage"] = {
                                        "prompt_tokens": prompt_tokens,
                                        "completion_tokens": completion_tokens,
                                        "total_tokens": prompt_tokens + completion_tokens
                                }

                        _finalize_upstream_response(response)
                        return utils.request.response(jsonify(result))

        except Exception as e:
                log.error(traceback.format_exc())
                if response is not None:
                        _finalize_upstream_response(response, error=str(e))
                return utils.request.response(jsonify({
                        "error": 500,
                        "message": "错误: " + str(e)
                })), 500

@app.route("/v1/messages", methods=["GET", "POST", "OPTIONS"])
def Anthropic_Compatible():
        response = None
        try:
                if request.method == "OPTIONS":
                        return utils.request.response(make_response())

                auth_error = _require_api_auth()
                if auth_error:
                        return auth_error

                odata = request.get_json(force=True, silent=True) or {}
                log.debug("收到请求:")
                log.debug("  data: %s", json.dumps(odata))
                id = utils.request.id("chat")
                stream = odata.get("stream", False)

                data = {
                        **utils.request.format(odata, "Anthropic"),
                        "chat_id": id,
                        "id": utils.request.id(),
                }
                requested_model = (odata.get("model") or cfg.model.default or "").strip() or cfg.model.default
                variant_config = MODEL_VARIANT_CONFIG.get(requested_model)
                if variant_config:
                        upstream_model = variant_config.get("upstream_id") or requested_model
                        data["model"] = upstream_model
                        features = data.setdefault("features", {})
                        for key, value in variant_config.get("features", {}).items():
                                features[key] = value
                        existing_servers: List[str] = []
                        if isinstance(odata.get("mcp_servers"), list):
                                existing_servers = [str(server) for server in odata.get("mcp_servers") if isinstance(server, str)]
                        for server in variant_config.get("mcp_servers", []):
                                if server not in existing_servers:
                                        existing_servers.append(server)
                        if existing_servers:
                                data["mcp_servers"] = existing_servers
                        model = requested_model
                else:
                        model = data.get("model", requested_model)
                messages = data.get("messages", [])
        
                # Anthropic 流式协议要求 message_start 中包含 input_tokens，所以必须计算
                prompt_tokens = utils.response.count("".join(
                    c if isinstance(c, str) else (c.get("text", "") if isinstance(c, dict) and c.get("type") == "text" else "")
                    for m in messages
                    for c in ([m["content"]] if isinstance(m.get("content"), str) else (m.get("content") or []))
                ))
        
                response = utils.request.chat(data, id)
                if response.status_code != 200:
                        _finalize_upstream_response(response, error=response.text or f"HTTP {response.status_code}")
                        return utils.request.response(jsonify({
                                "error": response.status_code,
                                "message": response.text or None
                        })), response.status_code

                if stream:
                        def generate_stream():
                                error_message: Optional[str] = None

                                def _inner():
                                        text_parts = []
                                        tool_call_parts = []
                                        has_tool_call = False

                                        # message_start
                                        yield "event: message_start\n"
                                        yield f"data: {json.dumps({
                                                'type': 'message_start',
                                                'message': {
                                                        'id': utils.request.id(),
                                                        'type': 'message',
                                                        'role': 'assistant',
                                                        'model': model,
                                                        'stop_reason': None,
                                                        'stop_sequence': None,
                                                        'usage': {
                                                                'input_tokens': prompt_tokens,
                                                                'output_tokens': 0
                                                        }
                                                }
                                        })}\n\n"

                                        yield "event: content_block_start\n"
                                        yield f"data: {json.dumps({
                                                'type': 'content_block_start',
                                                'index': 0,
                                                'content_block': {'type': 'text', 'text': ''}
                                        })}\n\n"

                                        yield "event: ping\n"
                                        yield f"data: {json.dumps({'type': 'ping'})}\n\n"

                                        # 流式解析
                                        for raw_chunk in utils.response.parse(response):
                                                if raw_chunk.get("data", {}).get("done"):
                                                        break
                                                delta = utils.response.format(raw_chunk, "Anthropic")
                                                if not delta:
                                                        continue

                                                if "tool_call" in delta:
                                                        tool_call_parts.append(delta["tool_call"])
                                                        tool_call_str = "".join(tool_call_parts)
                                                        try:
                                                                tool_json = json.loads(tool_call_str)
                                                                if "arguments" in tool_json:
                                                                        try:
                                                                                tool_json["input"] = json.loads(tool_json["arguments"])
                                                                        except (json.JSONDecodeError, TypeError):
                                                                                log.warning("arguments 无法解析为 JSON，保留原值: %s", tool_json["arguments"])
                                                                        del tool_json["arguments"]

                                                                log.debug("完整！调用！: %s", tool_json)
                                                                has_tool_call = True

                                                                yield "event: content_block_stop\n"
                                                                yield f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

                                                                yield "event: content_block_start\n"
                                                                yield f"data: {json.dumps({
                                                                        'type': 'content_block_start',
                                                                        'index': 1,
                                                                        'content_block': {
                                                                                'type': 'tool_use',
                                                                                **tool_json,
                                                                                "input": None
                                                                        }
                                                                })}\n\n"

                                                                if tool_json.get("input"):
                                                                        input_json_str = json.dumps(tool_json["input"])
                                                                        chunk_size = 5
                                                                        for i in range(0, len(input_json_str), chunk_size):
                                                                                chunk = input_json_str[i:i + chunk_size]
                                                                                yield "event: content_block_delta\n"
                                                                                yield f"data: {json.dumps({
                                                                                        'type': 'content_block_delta',
                                                                                        'index': 1,
                                                                                        'delta': {
                                                                                                'type': 'input_json_delta',
                                                                                                'partial_json': chunk
                                                                                        }
                                                                                })}\n\n"

                                                                yield "event: content_block_stop\n"
                                                                yield f"data: {json.dumps({'type': 'content_block_stop', 'index': 1})}\n\n"
                                                                break

                                                        except json.JSONDecodeError:
                                                                continue
                                                        except Exception as exc:
                                                                raise Exception(f"tool call parse fail: {exc}")

                                                if "text" in delta:
                                                        text_parts.append(delta["text"])
                                                        yield "event: content_block_delta\n"
                                                        yield f"data: {json.dumps({
                                                                'type': 'content_block_delta',
                                                                'index': 0,
                                                                'delta': {'type': 'text_delta', 'text': delta['text']}
                                                        })}\n\n"

                                        completion_str = "".join(text_parts)
                                        completion_tokens = utils.response.count(completion_str)

                                        if not has_tool_call:
                                                yield "event: content_block_stop\n"
                                                yield f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

                                                yield "event: message_delta\n"
                                                yield f"data: {json.dumps({
                                                        'type': 'message_delta',
                                                        'delta': {
                                                                'stop_reason': 'end_turn',
                                                                'stop_sequence': None
                                                        },
                                                        'usage': {
                                                                'output_tokens': completion_tokens
                                                        }
                                                })}\n\n"
                                        else:
                                                yield "event: message_delta\n"
                                                yield f"data: {json.dumps({
                                                        'type': 'message_delta',
                                                        'delta': {
                                                                'stop_reason': 'tool_use',
                                                                'stop_sequence': None
                                                        },
                                                        'usage': {
                                                                'output_tokens': completion_tokens
                                                        }
                                                })}\n\n"

                                        yield "event: message_stop\n"
                                        yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"

                                try:
                                        yield from _inner()
                                except GeneratorExit:
                                        error_message = "client disconnected"
                                        raise
                                except Exception as exc:
                                        error_message = str(exc)
                                        raise
                                finally:
                                        _finalize_upstream_response(response, error=error_message)

                        return Response(generate_stream(), mimetype="text/event-stream")

                else:
                        # 伪 - 非流式
                        text_parts = []
                        tool_call_parts = []

                        for raw_chunk in utils.response.parse(response):
                                if raw_chunk.get("data", {}).get("done"):
                                        break
                                delta = utils.response.format(raw_chunk)
                                if not delta:
                                        continue

                                if "tool_call" in delta:
                                        tool_call_parts.append(delta["tool_call"])
                                        tool_call_str = "".join(tool_call_parts)
                                        try:
                                                tool_json = json.loads(tool_call_str)
                                                if "arguments" in tool_json:
                                                        try:
                                                                tool_json["input"] = json.loads(tool_json["arguments"])
                                                                del tool_json["arguments"]
                                                        except (json.JSONDecodeError, TypeError):
                                                                log.warning("arguments 无法解析为 JSON，保留原值: %s", tool_json["arguments"])

                                                log.debug("完整！调用！: %s", tool_json)
                                                completion_str = "".join(text_parts)
                                                completion_tokens = utils.response.count(completion_str)

                                                content_blocks: List[Dict[str, Any]] = []
                                                if completion_str:
                                                        content_blocks.append({"type": "text", "text": completion_str})
                                                content_blocks.append({"type": "tool_use", **tool_json})

                                                result = {
                                                        "id": utils.request.id(),
                                                        "type": "message",
                                                        "role": "assistant",
                                                        "model": model,
                                                        "content": content_blocks,
                                                        "usage": {
                                                                "input_tokens": prompt_tokens,
                                                                "output_tokens": completion_tokens
                                                        },
                                                        "stop_sequence": None,
                                                        "stop_reason": "tool_use",
                                                }

                                                _finalize_upstream_response(response)
                                                return utils.request.response(jsonify(result))

                                        except json.JSONDecodeError:
                                                continue
                                        except Exception as exc:
                                                raise Exception(f"tool call parse fail: {exc}")

                                if "content" in delta:
                                        text_parts.append(delta["content"])
                                if "reasoning_content" in delta:
                                        text_parts.append(delta["reasoning_content"])

                        # 无 tool_call，纯文本
                        completion_str = "".join(text_parts)
                        completion_tokens = utils.response.count(completion_str)

                        result = {
                                "id": utils.request.id(),
                                "type": "message",
                                "role": "assistant",
                                "model": model,
                                "content": [{"type": "text", "text": completion_str}] if completion_str else [],
                                "usage": {
                                        "input_tokens": prompt_tokens,
                                        "output_tokens": completion_tokens
                                },
                                "stop_sequence": None,
                                "stop_reason": "end_turn",
                        }

                        _finalize_upstream_response(response)
                        return utils.request.response(jsonify(result))

        except Exception as e:
                log.error(traceback.format_exc())
                if response is not None:
                        _finalize_upstream_response(response, error=str(e))
                return utils.request.response(jsonify({
                        "error": 500,
                        "message": "错误: " + str(e)
                })), 500

# 健康检查
@app.route("/health")
def health():
    return utils.request.response(jsonify({
        "status": "ok",
        "timestamp": int(datetime.now().timestamp() * 1000)
    }))

# 主入口
if __name__ == "__main__":
    log.info("---------------------------------------------------------------------")
    log.info("Z.ai 2 API https://github.com/hmjz100/Z.ai2api")
    log.info("将 Z.ai 代理为 OpenAI/Anthropic Compatible 格式")
    log.info("基于 https://github.com/kbykb/OpenAI-Compatible-API-Proxy-for-Z 重构")
    log.info("---------------------------------------------------------------------")
    log.info("请稍后，正在检查网络……")
    models = utils.request.models()
    log.info("---------------------------------------------------------------------")
    log.info(f"Base           {cfg.source.protocol}//{cfg.source.host}")
    log.info("Models         /v1/models")
    log.info("OpenAI         /v1/chat/completions")
    log.info("Anthropic      /v1/messages")
    log.info("---------------------------------------------------------------------")
    log.info("服务端口：%s", cfg.api.port)
    log.info("可用模型：%s", ", ".join([item["id"] for item in models.get("data", []) if "id" in item]))
    log.info("备选模型：%s", cfg.model.default)
    log.info("思考处理：%s", cfg.api.think)
    log.info("访客模式：%s", cfg.api.anon)
    log.info("调试模式：%s", cfg.api.debug)
    log.info("调试信息：%s", cfg.api.debug_msg)
    
    if cfg.api.debug:
        app.run(host="0.0.0.0", port=cfg.api.port, threaded=True, debug=True)
    else:
        from gevent import pywsgi
        pywsgi.WSGIServer(('0.0.0.0', cfg.api.port), app).serve_forever()