# -*- coding: utf-8 -*-
"""
Z.ai 2 API
将 Z.ai 代理为 OpenAI Compatible 格式，支持免令牌、智能处理思考链、图片上传（仅登录后）等功能
基于 https://github.com/kbykb/OpenAI-Compatible-API-Proxy-for-Z 使用 AI 辅助重构。
"""

import os, json, re, requests, logging, uuid, base64, time, hashlib, hmac
from collections import defaultdict, deque
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from flask import (
        Flask,
        Response,
        jsonify,
        make_response,
        redirect,
        render_template_string,
        request,
        session,
        url_for,
        g,
        has_request_context,
)

from dotenv import load_dotenv
load_dotenv()

# 配置
BASE = str(os.getenv("BASE", "https://chat.z.ai"))
PORT = int(os.getenv("PORT", "8080"))
MODEL = str(os.getenv("MODEL", "GLM-4.5"))
TOKEN = str(os.getenv("TOKEN", "")).strip()
DEBUG_MODE = str(os.getenv("DEBUG", "false")).lower() == "true"
THINK_TAGS_MODE = str(os.getenv("THINK_TAGS_MODE", "reasoning"))
ANONYMOUS_MODE = str(os.getenv("ANONYMOUS_MODE", "true")).lower() == "true"
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

# tiktoken 预加载
cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiktoken') + os.sep
os.environ["TIKTOKEN_CACHE_DIR"] = cache_dir
assert os.path.exists(os.path.join(cache_dir, "9b5ad71b2ce5302211f9c61530b329a4922fc6a4")) # cl100k_base.tiktoken
import tiktoken
enc = tiktoken.get_encoding("cl100k_base")

BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "X-FE-Version": "prod-fe-1.0.76",
        "sec-ch-ua": '"Not;A=Brand";v="99", "Edge";v="139"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Origin": BASE,
}


def _shorten_token(token: str, head: int = 4, tail: int = 4) -> str:
        if not token:
                return ""
        if len(token) <= head + tail + 3:
                return token
        return f"{token[:head]}...{token[-tail:]}"


def _client_ip() -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
                return forwarded.split(",")[0].strip()
        return request.remote_addr or ""


def _format_upstream_path(url: str) -> str:
        if not url:
                return ""
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
                path = f"{path}?{parsed.query}"
        if parsed.netloc:
                return f"{parsed.netloc}{path}"
        return url


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
                                items.append({
                                        "token_id": _token_identifier(token),
                                        "display": _shorten_token(token),
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


TOKEN_POOL_TOKENS = _parse_token_pool(RAW_TOKEN_POOL)
for token in _persisted_tokens:
        if token not in TOKEN_POOL_TOKENS:
                TOKEN_POOL_TOKENS.append(token)
if TOKEN and TOKEN not in TOKEN_POOL_TOKENS:
        TOKEN_POOL_TOKENS.append(TOKEN)

token_pool = TokenPool(TOKEN_POOL_TOKENS, TOKEN_POOL_FAILURE_THRESHOLD)
_persist_token_pool(token_pool.tokens())


def _update_token_pool(tokens: List[str]):
        token_pool.update(tokens)
        _persist_token_pool(token_pool.tokens())


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
                elif token_source == "pool" or token_source == "static":
                        token_display = _shorten_token(token_value)
                        token_id = _token_identifier(token_value)
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
                                "timestamp": datetime.now().isoformat(timespec="seconds"),
                                "method": method,
                                "path": path,
                                "status": status_code if status_code is not None else 0,
                                "duration_ms": round(duration * 1000, 2),
                                "client_ip": client_ip,
                                "token_display": token_display,
                                "token_source": token_source,
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


def _record_upstream_metrics(
        *,
        method: str,
        url: str,
        status_code: Optional[int],
        duration: float,
        error: Optional[str] = None,
        token_info: Optional[Dict[str, Any]] = None,
):
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


def _has_valid_auth() -> bool:
        if not AUTH_TOKEN:
                return True
        if session.get("authenticated"):
                return True
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
        return candidate == AUTH_TOKEN


def _require_api_auth():
        if _has_valid_auth():
                return None
        response = jsonify({"error": "unauthorized"})
        return utils.request.response(make_response(response, 401))


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
        <div class=\"status\">服务运行中</div>
        <div class=\"meta\">
            <span>当前时间：{{ timestamp }}</span>
            <span>匿名模式：{{ '启用' if anonymous_mode else '关闭' }}</span>
            <span>Token 池容量：{{ token_count }}</span>
        </div>
        <p>代理已经启动，可以通过 OpenAI 兼容接口访问。{% if requires_auth %}已启用访问令牌校验，请在请求头或查询参数中携带 AUTH_TOKEN。{% endif %}</p>
        <p>管理与监控：<a href=\"/dashboard\">打开 Dashboard</a></p>
        <p>Token 池状态：<a href=\"/token-pool/status\">查看 JSON</a></p>
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
    <title>Z.ai 2 API 监控面板</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #0b0f1a;
            --card: rgba(17, 24, 39, 0.72);
            --border: rgba(148, 163, 184, 0.25);
            --accent: #22d3ee;
            --accent-muted: rgba(34, 211, 238, 0.2);
            --text: #f8fafc;
            --muted: #94a3b8;
            --danger: #f87171;
            --success: #34d399;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: 'Inter', 'PingFang SC', system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at top, rgba(56, 189, 248, 0.12), transparent 45%), var(--bg);
            color: var(--text);
            min-height: 100vh;
        }
        header {
            padding: 24px 32px 0 32px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
        }
        header h1 {
            margin: 0;
            font-size: 26px;
            font-weight: 600;
        }
        header .tag {
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
        }
        form#add-token-form input {
            flex: 1;
            min-width: 220px;
            padding: 10px 14px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: rgba(15, 23, 42, 0.65);
            color: var(--text);
            font-size: 14px;
        }
        .help-text { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
        @media (max-width: 768px) {
            header, main { padding: 24px 20px; }
            header { flex-direction: column; align-items: flex-start; }
            .section-header { flex-direction: column; align-items: flex-start; }
            form#add-token-form { width: 100%; }
            form#add-token-form input { width: 100%; }
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
                <form id=\"add-token-form\">
                    <input id=\"new-token\" name=\"token\" placeholder=\"粘贴新 Token\" autocomplete=\"off\" required />
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
        let timer = null;

        const escapeHtml = (value) => {
            if (value === undefined || value === null) return '';
            return String(value).replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
        };

        function renderRequests(list) {
            requestsBody.innerHTML = list.map(item => `
                <tr>
                    <td>${escapeHtml(item.timestamp)}</td>
                    <td>${escapeHtml(item.method)}</td>
                    <td>${escapeHtml(item.path)}</td>
                    <td><span class=\"badge ${item.status >= 400 ? 'danger' : 'success'}\">${item.status}</span></td>
                    <td>${item.duration_ms}</td>
                    <td>${escapeHtml(item.client_ip)}</td>
                    <td>${escapeHtml(item.token_display)}</td>
                </tr>
            `).join('');
        }

        function buildTokenRows(tokenStats, tokenPool) {
            const rows = [];
            const statsMap = new Map();
            tokenStats.forEach(item => {
                const key = item.token_id || `__${item.source}__`;
                statsMap.set(key, item);
            });

            const seen = new Set();
            tokenPool.tokens.forEach(item => {
                const stat = statsMap.get(item.token_id) || { display: item.display, source: 'pool', success: 0, failure: 0 };
                rows.push({
                    token_id: item.token_id,
                    display: item.display,
                    source: stat.source || 'pool',
                    success: stat.success || 0,
                    failure: stat.failure || 0,
                    disabled: !!item.disabled,
                    cooldown: item.cooldown_seconds || 0,
                });
                seen.add(item.token_id);
            });

            statsMap.forEach((stat, key) => {
                if (key.startsWith('__')) {
                    if (stat.source === 'anonymous') {
                        rows.push({
                            token_id: null,
                            display: stat.display || '匿名 token',
                            source: 'anonymous',
                            success: stat.success || 0,
                            failure: stat.failure || 0,
                            disabled: false,
                            cooldown: 0,
                        });
                    }
                    return;
                }
                if (!seen.has(key)) {
                    rows.push({
                        token_id: key,
                        display: stat.display || 'Token',
                        source: stat.source || 'static',
                        success: stat.success || 0,
                        failure: stat.failure || 0,
                        disabled: false,
                        cooldown: 0,
                    });
                }
            });

            return rows;
        }

        function renderTokens(tokenStats, tokenPool) {
            const rows = buildTokenRows(tokenStats, tokenPool);
            tokensBody.innerHTML = rows.map(item => {
                const status = item.source === 'anonymous' ? '匿名' : (item.disabled ? '禁用' : '活跃');
                const actionButton = item.token_id ? `<button type=\"button\" class=\"ghost\" data-token-id=\"${escapeHtml(item.token_id)}\">移除</button>` : '';
                return `
                    <tr>
                        <td>${escapeHtml(item.display)}</td>
                        <td>${escapeHtml(item.source)}</td>
                        <td>${item.success}</td>
                        <td>${item.failure}</td>
                        <td>${escapeHtml(status)}</td>
                        <td>${item.cooldown}</td>
                        <td>${actionButton}</td>
                    </tr>
                `;
            }).join('');
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

        async function fetchOverview() {
            const res = await fetch('/dashboard/api/overview', { credentials: 'same-origin' });
            if (res.status === 401) {
                window.location.reload();
                return;
            }
            const data = await res.json();
            totalEl.textContent = data.stats.total_requests;
            successEl.textContent = data.stats.success_requests;
            failureEl.textContent = data.stats.failure_requests;
            averageEl.textContent = data.stats.average_response_time;
            modeIndicator.textContent = data.anonymous_mode ? '匿名模式' : '非匿名模式';
            renderRequests(data.recent_requests);
            renderTokens(data.token_stats, data.token_pool);
            lastUpdated.textContent = `更新于 ${new Date().toLocaleTimeString()}`;
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
            event.preventDefault();
            const tokens = newTokenInput.value.split(/[\n,]/).map(item => item.trim()).filter(Boolean);
            if (!tokens.length) return;
            const res = await fetch('/dashboard/api/tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ tokens }),
            });
            if (res.ok) {
                newTokenInput.value = '';
                fetchOverview();
            } else {
                alert('添加失败，请检查日志');
            }
        });

        tokensBody.addEventListener('click', async (event) => {
            const button = event.target.closest('button[data-token-id]');
            if (!button) return;
            const tokenId = button.getAttribute('data-token-id');
            if (!confirm('确认移除该 Token 吗？')) return;
            const res = await fetch('/dashboard/api/tokens', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ token_id: tokenId }),
            });
            if (res.ok) {
                fetchOverview();
            } else {
                alert('移除失败，请重试');
            }
        });

        if (logoutBtn) {
            logoutBtn.addEventListener('click', async () => {
                await fetch('/dashboard/logout', { method: 'POST', credentials: 'same-origin' });
                window.location.reload();
            });
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
        body {
            margin: 0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: radial-gradient(circle at top, rgba(34, 211, 238, 0.18), transparent 55%), #0b0f1a;
            font-family: 'Inter', 'PingFang SC', system-ui, sans-serif;
            color: #f8fafc;
        }
        form {
            width: min(360px, 90vw);
            background: rgba(15, 23, 42, 0.85);
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 16px;
            padding: 32px 28px;
            box-shadow: 0 18px 36px rgba(8, 15, 28, 0.55);
        }
        h1 { margin: 0 0 24px; font-size: 22px; text-align: center; }
        label { display: block; font-size: 14px; margin-bottom: 8px; color: #cbd5f5; }
        input[type=password] {
            width: 100%;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid rgba(148, 163, 184, 0.25);
            background: rgba(15, 23, 42, 0.65);
            color: #f8fafc;
            font-size: 15px;
            outline: none;
        }
        button {
            width: 100%;
            margin-top: 20px;
            padding: 12px;
            border-radius: 10px;
            border: none;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            background: linear-gradient(135deg, #22d3ee, #818cf8);
            color: #0b1120;
        }
        .error {
            margin-top: 12px;
            padding: 10px;
            border-radius: 10px;
            background: rgba(248, 113, 113, 0.18);
            color: #fecaca;
            font-size: 13px;
        }
        .hint { margin-top: 16px; font-size: 13px; color: #94a3b8; text-align: center; }
    </style>
</head>
<body>
    <form method=\"post\">
        <h1>登录监控面板</h1>
        {% if error %}<div class=\"error\">{{ error }}</div>{% endif %}
        {% if not auth_required %}<div class=\"hint\">当前未设置 AUTH_TOKEN，将直接访问面板。</div>{% endif %}
        {% if auth_required %}
        <label for=\"password\">访问密码 (AUTH_TOKEN)</label>
        <input type=\"password\" id=\"password\" name=\"password\" placeholder=\"请输入 AUTH_TOKEN\" autocomplete=\"current-password\" required />
        <button type=\"submit\">登录</button>
        {% else %}
        <button type=\"submit\">进入仪表盘</button>
        {% endif %}
        <div class=\"hint\">需要通过 AUTH_TOKEN 保护仪表盘安全。</div>
    </form>
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

                thinking_mcp_servers = meta.get("thinking_mcp_servers")
                if thinking_mcp_servers is None:
                        thinking_mcp_servers = base_entry["mcp_servers"]

                config[f"{alias}-Thinking"] = {
                        "upstream_id": upstream_id,
                        "description": meta.get("thinking_description") or f"{alias} 思考模型",
                        "features": thinking_features,
                        "mcp_servers": list(thinking_mcp_servers),
                }

                search_features = dict(base_features)
                search_features["web_search"] = True
                search_features["auto_web_search"] = True

                search_mcp_servers = meta.get("search_mcp_servers")
                if search_mcp_servers is None:
                        search_mcp_servers = DEFAULT_SEARCH_MCP_SERVERS

                config[f"{alias}-Search"] = {
                        "upstream_id": upstream_id,
                        "description": meta.get("search_description") or f"{alias} 搜索模型",
                        "features": search_features,
                        "mcp_servers": list(search_mcp_servers),
                }

        return config


MODEL_VARIANT_CONFIG = _build_model_variant_config(BASE_MODEL_VARIANT_DEFINITIONS)

# 日志
logging.basicConfig(level=logging.DEBUG if DEBUG_MODE else logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

def debug(msg, *args):
        if DEBUG_MODE: log.debug(msg, *args)

# Flask 应用
app = Flask(__name__)
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
                @staticmethod
                def chat(data, chat_id):
                        debug("收到请求: %s", json.dumps(data))
                        token = utils.request.token()
                        headers = {**BROWSER_HEADERS, "Referer": f"{BASE}/c/{chat_id}"}
                        if token:
                                headers["Authorization"] = f"Bearer {token}"
                        url = f"{BASE}/api/chat/completions"
                        start_time = time.perf_counter()
                        try:
                                response = requests.post(
                                        url,
                                        json=data,
                                        headers=headers,
                                        stream=True,
                                        timeout=60
                                )
                        except Exception as e:
                                duration = time.perf_counter() - start_time
                                _record_upstream_metrics(
                                        method="POST",
                                        url=url,
                                        status_code=None,
                                        duration=duration,
                                        error=str(e),
                                )
                                if token_pool.contains(token):
                                        token_pool.mark_failure(token)
                                raise e

                        response._metrics_context = {
                                "start_time": start_time,
                                "url": url,
                                "method": "POST",
                                "token": token,
                                "token_info": dict(getattr(g, "current_token_info", {}) or {}),
                                "finalized": False,
                        }
                        return response
                @staticmethod
                def image(data_url, chat_id):
                        try:
                                if ANONYMOUS_MODE or not data_url.startswith("data:"):
                                        return None

                                header, encoded = data_url.split(",", 1)
                                mime_type = header.split(";")[0].split(":")[1] if ":" in header else "image/jpeg"

                                image_data = base64.b64decode(encoded) # 解码数据
                                filename = str(uuid.uuid4())

                                debug("上传文件：%s", filename)
                                token = utils.request.token(prefer_pool=True)
                                headers = {**BROWSER_HEADERS, "Referer": f"{BASE}/c/{chat_id}"}
                                if token:
                                        headers["Authorization"] = f"Bearer {token}"

                                url = f"{BASE}/api/v1/files/"
                                start_time = time.perf_counter()
                                recorded = False
                                response = requests.post(
                                        url,
                                        files={"file": (filename, image_data, mime_type)},
                                        headers=headers,
                                        timeout=30
                                )
                                duration = time.perf_counter() - start_time
                                _record_upstream_metrics(
                                        method="POST",
                                        url=url,
                                        status_code=response.status_code,
                                        duration=duration,
                                )
                                recorded = True

                                if token_pool.contains(token):
                                        if response.status_code in (401, 403):
                                                token_pool.mark_failure(token)
                                        else:
                                                token_pool.mark_success(token)

                                if response.status_code == 200:
                                        result = response.json()
                                        return f"{result.get("id")}_{result.get("filename")}"
                                else:
                                        raise Exception(response.text)
                        except Exception as e:
                                if 'start_time' in locals() and not recorded:
                                        duration = time.perf_counter() - start_time
                                        _record_upstream_metrics(
                                                method="POST",
                                                url=url,
                                                status_code=None,
                                                duration=duration,
                                                error=str(e),
                                        )
                                debug("图片上传失败: %s", e)
                        return None
                @staticmethod
                def id(prefix = "msg") -> str:
                        return f"{prefix}-{int(datetime.now().timestamp()*1e9)}"
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

                        if not ANONYMOUS_MODE and TOKEN:
                                source = "static"
                                g.current_token_info = {"token": TOKEN, "source": source}
                                return TOKEN

                        try:
                                r = requests.get(f"{BASE}/api/v1/auths/", headers=BROWSER_HEADERS, timeout=8)
                                token = r.json().get("token")
                                if token:
                                        source = "anonymous"
                                        g.current_token_info = {"token": token, "source": source}
                                        debug("获取匿名令牌: %s...", token[:15])
                                        return token
                        except Exception as e:
                                debug("匿名令牌获取失败: %s", e)

                        fallback_token = TOKEN or token
                        if fallback_token:
                                source = "static" if fallback_token == TOKEN else "anonymous"
                        else:
                                source = "anonymous"
                        g.current_token_info = {"token": fallback_token, "source": source}
                        return fallback_token or ""
                @staticmethod
                def response(resp):
                        resp.headers.update({
                                "Access-Control-Allow-Origin": "*",
                                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                        })
                        return resp
        @staticmethod
        class response:
                @staticmethod
                def parse(stream):
                        for line in stream.iter_lines():
                                if not line or not line.startswith(b"data: "): continue
                                try: data = json.loads(line[6:].decode("utf-8", "ignore"))
                                except: continue
                                yield data
                @staticmethod
                def format(data):
                        data = data.get("data", "")
                        if not data: return None
                        phase = data.get("phase", "other")
                        content = data.get("delta_content") or data.get("edit_content") or ""
                        if not content: return None
                        contentBak = content
                        global phaseBak
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

                                if THINK_TAGS_MODE == "reasoning":
                                        if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                                        content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                                        content = re.sub(r"<reasoning>\n*", "", content)
                                        content = re.sub(r"\n*</reasoning>", "", content)
                                elif THINK_TAGS_MODE == "think":
                                        if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                                        content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                                        content = re.sub(r"<reasoning>", "<think>", content)
                                        content = re.sub(r"</reasoning>", "</think>", content)
                                elif THINK_TAGS_MODE == "strip":
                                        content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                                        content = re.sub(r"<reasoning>\n*", "", content)
                                        content = re.sub(r"</reasoning>", "", content)
                                elif THINK_TAGS_MODE == "details":
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
                                        debug("警告：THINK_TAGS_MODE 传入了未知的替换模式，将使用 <reasoning> 标签。")

                        phaseBak = phase
                        if repr(content) != repr(contentBak):
                                debug("R 内容: %s %s", phase, repr(contentBak))
                                debug("W 内容: %s %s", phase, repr(content))
                        else:
                                debug("R 内容: %s %s", phase, repr(contentBak))

                        if phase == "thinking" and THINK_TAGS_MODE == "reasoning":
                                return {"role": "assistant", "reasoning_content": content}
                        elif repr(content):
                                return {"role": "assistant", "content":content}
                        else:
                                return None
                @staticmethod
                def count(text):
                        return len(enc.encode(text))

# 路由
@app.route("/", methods=["GET"])
def service_status_page():
        snapshot = token_pool.snapshot()
        token_count = snapshot.get("size", 0)
        return render_template_string(
                STATUS_PAGE_TEMPLATE,
                timestamp=datetime.now().isoformat(timespec="seconds"),
                anonymous_mode=ANONYMOUS_MODE,
                token_count=token_count,
                requires_auth=bool(AUTH_TOKEN),
        )


@app.route("/v1/models", methods=["GET", "POST", "OPTIONS"])
def models():
    if request.method == "OPTIONS":
        return utils.request.response(make_response())
    auth_error = _require_api_auth()
    if auth_error:
        return auth_error
    try:
        def format_model_name(name: str) -> str:
            """格式化模型名:
            - 单段: 全大写
            - 多段: 第一段全大写, 后续段首字母大写
            - 数字保持不变, 符号原样保留
            """

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

        def is_english_letter(ch: str) -> bool:
            """判断是否是英文字符 (A-Z / a-z)"""

            return 'A' <= ch <= 'Z' or 'a' <= ch <= 'z'

        token = utils.request.token()
        headers = {**BROWSER_HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url = f"{BASE}/api/models"
        start_time = time.perf_counter()
        recorded = False
        response = requests.get(url, headers=headers, timeout=8)
        duration = time.perf_counter() - start_time
        _record_upstream_metrics(
            method="GET",
            url=url,
            status_code=response.status_code,
            duration=duration,
        )
        recorded = True
        if token_pool.contains(token):
            if response.status_code in (401, 403):
                token_pool.mark_failure(token)
            else:
                token_pool.mark_success(token)

        r = response.json()
        models = []
        existing_ids = set()
        upstream_created_map: Dict[str, int] = {}

        for m in r.get("data", []):
            if not m.get("info", {}).get("is_active", True):
                continue

            model_id, model_name = m.get("id"), m.get("name")
            alias_name = MODEL_ID_ALIASES.get(model_id) if model_id else None
            if alias_name:
                model_name = alias_name
            elif model_id and model_id.startswith(("GLM", "Z")):
                model_name = model_id

            if not model_name or not is_english_letter(model_name[0]):
                model_name = format_model_name(model_id)

            created_at = m.get("info", {}).get("created_at", int(datetime.now().timestamp()))
            upstream_created_map[model_id] = created_at

            entry = {
                "id": model_id,
                "object": "model",
                "name": model_name,
                "created": created_at,
                "owned_by": "z.ai",
            }
            models.append(entry)
            existing_ids.add(entry["id"])

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
            models.append(entry)
            existing_ids.add(variant_name)

        return utils.request.response(jsonify({"object": "list", "data": models}))
    except Exception as e:
        if 'url' in locals() and 'start_time' in locals() and not recorded:
            duration = time.perf_counter() - start_time
            _record_upstream_metrics(
                method="GET",
                url=url,
                status_code=None,
                duration=duration,
                error=str(e),
            )
        debug("模型列表失败: %s", e)
        return utils.request.response(jsonify({"error": "fetch models failed"})), 500

@app.route("/v1/chat/completions", methods=["GET", "POST", "OPTIONS"])
def OpenAI_Compatible():
        if request.method == "OPTIONS": return utils.request.response(make_response())
        auth_error = _require_api_auth()
        if auth_error:
                return auth_error
        odata = request.get_json(force=True, silent=True) or {}

        id = utils.request.id("chat")
        requested_model = (odata.get("model") or MODEL or "").strip() or MODEL
        normalized_model = requested_model
        for variant in MODEL_VARIANT_CONFIG:
                if variant.lower() == requested_model.lower():
                        normalized_model = variant
                        break

        variant_config = MODEL_VARIANT_CONFIG.get(normalized_model)
        upstream_model = variant_config.get("upstream_id") if variant_config else normalized_model
        model = normalized_model if variant_config else requested_model
        messages = odata.get("messages", [])
        raw_features = odata.get("features")
        features: Dict[str, Any] = {
                "image_generation": False,
                "web_search": False,
                "auto_web_search": False,
                "preview_mode": False,
                "flags": [],
                "features": [],
                "enable_thinking": True,
        }
        if isinstance(raw_features, dict):
                for key, value in raw_features.items():
                        features[key] = value
        if variant_config:
                for key, value in variant_config.get("features", {}).items():
                        features[key] = value

        reasoning_flag = odata.get("reasoning")
        if isinstance(reasoning_flag, bool):
                features["enable_thinking"] = reasoning_flag

        for bool_key in ("enable_thinking", "web_search", "auto_web_search", "image_generation", "preview_mode"):
                if bool_key in features:
                        features[bool_key] = bool(features[bool_key])

        for list_key in ("flags", "features"):
                if list_key in features and not isinstance(features[list_key], list):
                        features[list_key] = []

        stream = odata.get("stream", False)
        include_usage = stream and odata.get("stream_options", {}).get("include_usage", False)

        mcp_servers: List[str] = []
        if isinstance(odata.get("mcp_servers"), list):
                for server in odata.get("mcp_servers"):
                        if isinstance(server, str) and server not in mcp_servers:
                                mcp_servers.append(server)
        if variant_config:
                for server in variant_config.get("mcp_servers", []):
                        if server not in mcp_servers:
                                mcp_servers.append(server)
        for message in messages:
                if isinstance(message.get("content"), list):
                        for content_item in message["content"]:
                                if content_item.get("type") == "image_url":
                                        url = content_item.get("image_url", {}).get("url", "")
                                        if url.startswith("data:"):
                                                file_url = utils.request.image(url, id) # 上传图片
                                                if file_url:
                                                        content_item["image_url"]["url"] = file_url # 上传后的图片链接

        data = {
                **odata,
                "stream": True,
                "chat_id": id,
                "id": utils.request.id(),
                "model": upstream_model,
                "messages": messages,
                "features": features
        }

        if mcp_servers:
                data["mcp_servers"] = mcp_servers

        data.setdefault("model_item", {"id": upstream_model, "name": model, "owned_by": "z.ai"})

        try:
                response = utils.request.chat(data, id)
        except Exception as e:
                return utils.request.response(make_response(f"上游请求失败: {e}", 502))

        prompt_tokens = utils.response.count("".join(
                c if isinstance(c, str) else (c.get("text", "") if isinstance(c, dict) and c.get("type") == "text" else "")
                for m in messages
                for c in ([m["content"]] if isinstance(m.get("content"), str) else (m.get("content") or []))
        ))
        if stream:
                def stream():
                        completion_str = ""
                        completion_tokens = 0
                        error_message: Optional[str] = None

                        if response.status_code and response.status_code >= 400:
                                try:
                                        error_body = response.text
                                except Exception:
                                        error_body = ""
                                error_body = (error_body or "").strip()
                                if len(error_body) > 500:
                                        error_body = f"{error_body[:497]}..."
                                if error_body:
                                        error_message = f"上游返回错误 {response.status_code}: {error_body}"
                                else:
                                        error_message = f"上游返回错误 {response.status_code}"
                                debug(error_message)
                        else:
                                try:
                                        for data in utils.response.parse(response):
                                                is_done = data.get("data", {}).get("done", False)
                                                delta = utils.response.format(data)
                                                finish_reason = "stop" if is_done else None

                                                if delta:
                                                        yield f"data: {json.dumps({
                                                                "id": utils.request.id('chatcmpl'),
                                                                "object": "chat.completion.chunk",
                                                                "created": int(datetime.now().timestamp()),
                                                                "model": model,
                                                                "choices": [
                                                                        {
                                                                                "index": 0,
                                                                                "delta": delta,
                                                                                "message": delta,
                                                                                "finish_reason": finish_reason
                                                                        }
                                                                ]
                                                        })}\n\n"

                                                        if "content" in delta:
                                                                completion_str += delta["content"]
                                                        if "reasoning_content" in delta:
                                                                completion_str += delta["reasoning_content"]
                                                        completion_tokens = utils.response.count(completion_str)

                                                if is_done:
                                                        yield f"data: {json.dumps({
                                                                'id': utils.request.id('chatcmpl'),
                                                                'object': 'chat.completion.chunk',
                                                                'created': int(datetime.now().timestamp()),
                                                                'model': model,
                                                                'choices': [
                                                                        {
                                                                                'index': 0,
                                                                                'delta': {"role": "assistant"},
                                                                                'message': {"role": "assistant"},
                                                                                'finish_reason': "stop"
                                                                        }
                                                                ]
                                                        })}\n\n"
                                                        break
                                except GeneratorExit:
                                        _finalize_upstream_response(response, error="client disconnected")
                                        raise
                                except requests.exceptions.ChunkedEncodingError as exc:
                                        error_message = f"上游响应中断: {exc}"
                                        debug(error_message)
                                except requests.exceptions.RequestException as exc:
                                        error_message = f"上游响应异常: {exc}"
                                        debug(error_message)
                                except Exception as exc:
                                        error_message = f"解析上游响应失败: {exc}"
                                        debug(error_message)

                        if error_message:
                                yield f"data: {json.dumps({
                                        "id": utils.request.id('chatcmpl'),
                                        "object": "chat.completion.chunk",
                                        "created": int(datetime.now().timestamp()),
                                        "model": model,
                                        "choices": [
                                                {
                                                        "index": 0,
                                                        "delta": {},
                                                        "message": {},
                                                        "finish_reason": "error"
                                                }
                                        ],
                                        "error": {"message": error_message}
                                })}\n\n"
                        elif include_usage:
                                yield f"data: {json.dumps({
                                        "id": utils.request.id('chatcmpl'),
                                        "object": "chat.completion.chunk",
                                        "created": int(datetime.now().timestamp()),
                                        "model": model,
                                        "choices": [],
                                        "usage": {
                                                "prompt_tokens": prompt_tokens,
                                                "completion_tokens": completion_tokens,
                                                "total_tokens": prompt_tokens + completion_tokens
                                        }
                                })}\n\n"

                        yield "data: [DONE]\n\n"
                        _finalize_upstream_response(response, error=error_message)

                return Response(stream(), mimetype="text/event-stream")
        else:
                # 上游不支持非流式，所以先用流式获取所有内容
                contents = {
                        "content": [],
                        "reasoning_content": []
                }
                error_message: Optional[str] = None
                if response.status_code and response.status_code >= 400:
                        try:
                                error_body = response.text
                        except Exception:
                                error_body = ""
                        error_body = (error_body or "").strip()
                        if len(error_body) > 500:
                                error_body = f"{error_body[:497]}..."
                        if error_body:
                                error_message = f"上游返回错误 {response.status_code}: {error_body}"
                        else:
                                error_message = f"上游返回错误 {response.status_code}"
                        debug(error_message)
                if not error_message:
                        try:
                                for odata in utils.response.parse(response):
                                        if odata.get("data", {}).get("done"):
                                                break
                                        delta = utils.response.format(odata)
                                        if delta:
                                                if "content" in delta:
                                                        contents["content"].append(delta["content"])
                                                if "reasoning_content" in delta:
                                                        contents["reasoning_content"].append(delta["reasoning_content"])
                        except requests.exceptions.ChunkedEncodingError as exc:
                                error_message = f"上游响应中断: {exc}"
                                debug(error_message)
                        except requests.exceptions.RequestException as exc:
                                error_message = f"上游响应异常: {exc}"
                                debug(error_message)
                        except Exception as exc:
                                error_message = f"解析上游响应失败: {exc}"
                                debug(error_message)

                if error_message:
                        _finalize_upstream_response(response, error=error_message)
                        payload = {
                                "error": {
                                        "message": error_message,
                                }
                        }
                        return utils.request.response(make_response(jsonify(payload), 502))

                # 构建最终消息内容
                final_message = {"role": "assistant"}
                completion_str = ""
                if contents["reasoning_content"]:
                        final_message["reasoning_content"] = "".join(contents["reasoning_content"])
                        completion_str += "".join(contents["reasoning_content"])
                if contents["content"]:
                        final_message["content"] = "".join(contents["content"])
                        completion_str += "".join(contents["content"])
                completion_tokens = utils.response.count(completion_str) # 计算 tokens

                # 返回 Flask 响应
                _finalize_upstream_response(response, error=None)
        return utils.request.response(jsonify({
                        "id": utils.request.id("chatcmpl"),
                        "object": "chat.completion",
                        "created": int(datetime.now().timestamp()),
                        "model": model,
                        "choices": [{
                                "index": 0,
                                "delta": final_message,
                                "message": final_message,
                                "finish_reason": "stop"
                        }],
                        "usage": {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens
                        }
                }))


@app.route("/token-pool/status", methods=["GET", "OPTIONS"])
def token_pool_status():
        if request.method == "OPTIONS":
                return utils.request.response(make_response())
        auth_error = _require_api_auth()
        if auth_error:
                return auth_error
        snapshot = token_pool.snapshot()
        metrics_snapshot = request_metrics.snapshot()
        payload = {
                "token_pool": snapshot,
                "token_stats": metrics_snapshot.get("token_stats", []),
        }
        return utils.request.response(jsonify(payload))


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
        error = None
        if request.method == "POST":
                if not AUTH_TOKEN:
                        session["authenticated"] = True
                        return redirect(url_for("dashboard"))
                password = (request.form.get("password") or "").strip()
                if password == AUTH_TOKEN:
                        session["authenticated"] = True
                        return redirect(url_for("dashboard"))
                error = "密码错误，请重试。"
        if not _has_valid_auth():
                return render_template_string(
                        DASHBOARD_LOGIN_TEMPLATE,
                        error=error,
                        auth_required=bool(AUTH_TOKEN),
                )
        return render_template_string(DASHBOARD_TEMPLATE, anonymous_mode=ANONYMOUS_MODE)


@app.route("/dashboard/logout", methods=["POST"])
def dashboard_logout():
        session.pop("authenticated", None)
        return utils.request.response(jsonify({"ok": True}))


@app.route("/dashboard/api/overview", methods=["GET"])
def dashboard_overview():
        auth_error = _require_api_auth()
        if auth_error:
                return auth_error
        metrics_snapshot = request_metrics.snapshot()
        token_snapshot = token_pool.snapshot()
        payload = {
                "anonymous_mode": ANONYMOUS_MODE,
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
        return utils.request.response(jsonify(payload))


@app.route("/dashboard/api/tokens", methods=["GET", "POST", "DELETE"])
def dashboard_tokens():
        auth_error = _require_api_auth()
        if auth_error:
                return auth_error
        if request.method == "GET":
                snapshot = token_pool.snapshot()
                metrics_snapshot = request_metrics.snapshot()
                return utils.request.response(jsonify({
                        "token_pool": snapshot,
                        "token_stats": metrics_snapshot.get("token_stats", []),
                }))

        data = request.get_json(force=True, silent=True) or {}
        token_inputs: List[str] = []
        for value in (data.get("tokens"), data.get("token")):
                token_inputs.extend(_normalize_token_inputs(value))
        token_id_inputs: List[str] = []
        for value in (data.get("token_ids"), data.get("token_id")):
                token_id_inputs.extend(_normalize_token_inputs(value))

        resolved_tokens: List[str] = []
        for identifier in token_id_inputs:
                resolved = token_pool.resolve_id(identifier)
                if resolved and resolved not in resolved_tokens:
                        resolved_tokens.append(resolved)

        if request.method == "POST":
                effective_tokens = list(dict.fromkeys(token_inputs))
        else:
                effective_tokens = list(dict.fromkeys(token_inputs + resolved_tokens))

        if not effective_tokens:
                return utils.request.response(make_response(jsonify({"error": "token required"}), 400))

        current_tokens = token_pool.tokens()
        if request.method == "POST":
                updated = False
                for candidate in effective_tokens:
                        if candidate not in current_tokens:
                                current_tokens.append(candidate)
                                updated = True
                if updated:
                        _update_token_pool(current_tokens)
                snapshot = token_pool.snapshot()
                metrics_snapshot = request_metrics.snapshot()
                return utils.request.response(jsonify({
                        "ok": True,
                        "token_pool": snapshot,
                        "token_stats": metrics_snapshot.get("token_stats", []),
                }))

        removed = False
        for candidate in effective_tokens:
                if candidate in current_tokens:
                        current_tokens.remove(candidate)
                        removed = True
        if removed:
                _update_token_pool(current_tokens)
        snapshot = token_pool.snapshot()
        metrics_snapshot = request_metrics.snapshot()
        return utils.request.response(jsonify({
                "ok": True,
                "token_pool": snapshot,
                "token_stats": metrics_snapshot.get("token_stats", []),
        }))

# 主入口
if __name__ == "__main__":
        log.info("---------------------------------------------------------------------")
        log.info("Z.ai 2 API")
        log.info("将 Z.ai 代理为 OpenAI Compatible 格式")
        log.info("基于 https://github.com/kbykb/OpenAI-Compatible-API-Proxy-for-Z 重构")
        log.info("---------------------------------------------------------------------")
        log.info("服务端口：%s", PORT)
        log.info("备选模型：%s", MODEL)
        log.info("思考处理：%s", THINK_TAGS_MODE)
        log.info("访客模式：%s", ANONYMOUS_MODE)
        log.info("显示调试：%s", DEBUG_MODE)
        app.run(host="0.0.0.0", port=PORT, threaded=True, debug=DEBUG_MODE)
