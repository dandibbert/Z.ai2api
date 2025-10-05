# -*- coding: utf-8 -*-
"""
Z.ai 2 API
将 Z.ai 代理为 OpenAI Compatible 格式，支持免令牌、智能处理思考链、图片上传（仅登录后）等功能
基于 https://github.com/kbykb/OpenAI-Compatible-API-Proxy-for-Z 使用 AI 辅助重构。
"""

import os, json, re, requests, logging, uuid, base64
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional
from flask import Flask, request, Response, jsonify, make_response

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

RAW_TOKEN_POOL = str(os.getenv("TOKEN_POOL", "")).strip()
TOKEN_POOL_FAILURE_THRESHOLD = int(os.getenv("TOKEN_POOL_FAILURE_THRESHOLD", "3"))
TOKEN_POOL_RESET_FAILURES = int(os.getenv("TOKEN_POOL_RESET_FAILURES", "1800"))

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

def _parse_token_pool(raw: str) -> List[str]:
	tokens: List[str] = []
	if not raw:
		return tokens
	for candidate in re.split(r"[\n,]", raw):
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
			self._failures.pop(token, None)
			self._disabled.pop(token, None)

	def mark_failure(self, token: Optional[str]):
		if not token or token not in self._tokens:
			return
		with self._lock:
			count = self._failures.get(token, 0) + 1
			self._failures[token] = count
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
			for token in list(self._failures.keys()):
				if token not in self._tokens:
					self._failures.pop(token, None)
					self._disabled.pop(token, None)

	def contains(self, token: Optional[str]) -> bool:
		return bool(token and token in self._tokens)


TOKEN_POOL_TOKENS = _parse_token_pool(RAW_TOKEN_POOL)
if TOKEN and TOKEN not in TOKEN_POOL_TOKENS:
	TOKEN_POOL_TOKENS.append(TOKEN)

token_pool = TokenPool(TOKEN_POOL_TOKENS, TOKEN_POOL_FAILURE_THRESHOLD)


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

phaseBak = "thinking"
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
			try:
				response = requests.post(
					f"{BASE}/api/chat/completions",
					json=data,
					headers=headers,
					stream=True,
					timeout=60
				)
				if token_pool.contains(token):
					if response.status_code in (401, 403):
						token_pool.mark_failure(token)
					else:
						token_pool.mark_success(token)
				return response
			except Exception as e:
				if token_pool.contains(token):
					token_pool.mark_failure(token)
				raise e
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

				response = requests.post(
					f"{BASE}/api/v1/files/",
					files={"file": (filename, image_data, mime_type)},
					headers=headers,
					timeout=30
				)

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
				debug("图片上传失败: %s", e)
			return None
		@staticmethod
		def id(prefix = "msg") -> str:
			return f"{prefix}-{int(datetime.now().timestamp()*1e9)}"
		@staticmethod
		def token(prefer_pool: bool = True) -> str:
			token: Optional[str] = None

			if prefer_pool:
				token = token_pool.get()
				if token:
					debug("使用池中令牌: %s...", token[:10])
					return token

			if not ANONYMOUS_MODE:
				if TOKEN:
					return TOKEN

			try:
				r = requests.get(f"{BASE}/api/v1/auths/", headers=BROWSER_HEADERS, timeout=8)
				token = r.json().get("token")
				if token:
					debug("获取匿名令牌: %s...", token[:15])
					return token
			except Exception as e:
				debug("匿名令牌获取失败: %s", e)
			return TOKEN
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
@app.route("/v1/models", methods=["GET", "POST", "OPTIONS"])
def models():
    if request.method == "OPTIONS":
        return utils.request.response(make_response())
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

        response = requests.get(f"{BASE}/api/models", headers=headers, timeout=8)
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
        debug("模型列表失败: %s", e)
        return utils.request.response(jsonify({"error": "fetch models failed"})), 500

@app.route("/v1/chat/completions", methods=["GET", "POST", "OPTIONS"])
def OpenAI_Compatible():
	if request.method == "OPTIONS": return utils.request.response(make_response())
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

			# 处理流式响应数据
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

					# 累积实际生成的内容
					if "content" in delta:
						completion_str += delta["content"]
					if "reasoning_content" in delta:
						completion_str += delta["reasoning_content"]
					completion_tokens = utils.response.count(completion_str) # 计算 tokens
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

			if include_usage:
				# 发送 usage 统计信息
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

			# 发送 [DONE] 标志，表示流结束
			yield "data: [DONE]\n\n"

		# 返回 Flask 的流式响应
		return Response(stream(), mimetype="text/event-stream")
	else:
		# 上游不支持非流式，所以先用流式获取所有内容
		contents = {
			"content": [],
			"reasoning_content": []
		}
		for odata in utils.response.parse(response):
			if odata.get("data", {}).get("done"):
				break
			delta = utils.response.format(odata)
			if delta:
				if "content" in delta:
					contents["content"].append(delta["content"])
				if "reasoning_content" in delta:
					contents["reasoning_content"].append(delta["reasoning_content"])

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