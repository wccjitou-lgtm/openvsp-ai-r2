"""OpenAI 兼容 HTTP 客户端（仅使用 Python 标准库 urllib）。

- ``list_models``  GET  ``{base_url}/models``
- ``chat``         POST ``{base_url}/chat/completions``（支持 tools / tool_calls）

所有错误统一为 :class:`ProviderError`，错误信息中会抹除 API key，绝不打印明文 key。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 60.0
USER_AGENT = "OpenVSP-R2-AI-Design/1.0"


class ProviderError(RuntimeError):
    """LLM 服务调用失败（网络 / HTTP 状态 / 协议解析）。"""

    def __init__(self, message: str, *, status_code: int | None = None,
                 url: str = "", body: str = "", kind: str = "error"):
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body
        self.kind = kind

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "kind": self.kind,
            "status_code": self.status_code,
            "url": self.url,
            "error": str(self),
            "body": self.body[:2000],
        }


def normalize_base_url(base_url: str) -> str:
    """把用户填写的 base_url 归一化成可拼接 ``/models`` 的前缀。"""
    value = (base_url or "").strip()
    if not value:
        raise ProviderError("base_url 为空", kind="config")
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme:
        value = "https://" + value
        parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ProviderError(f"base_url 协议不支持: {parsed.scheme}", kind="config")
    value = value.rstrip("/")
    # 允许用户直接粘贴完整 endpoint
    for suffix in ("/chat/completions", "/chat", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip("/")


def _redact(text: str, api_key: str) -> str:
    if not text:
        return text
    key = (api_key or "").strip()
    if key and key in text:
        text = text.replace(key, "***")
    return text


def _request_json(method: str, url: str, api_key: str,
                  payload: dict[str, Any] | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    # 429/5xx 为常见临时限流/服务波动（GLM 并发不足会返回 429），带退避重试。
    attempts = 3
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                snippet = raw[:500].decode("utf-8", errors="replace")
                raise ProviderError(
                    _redact(f"响应不是合法 JSON: {exc} (url={url})", api_key),
                    url=url, body=snippet, kind="protocol",
                ) from exc
        except urllib.error.HTTPError as exc:
            retryable = exc.code in (429, 500, 502, 503, 504)
            if retryable and attempt < attempts - 1:
                delay = 5.0 * (attempt + 1)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after:
                    try:
                        delay = max(1.0, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(min(delay, 30.0))
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass
            body = _redact(body, api_key)
            detail = _extract_error_message(body)
            raise ProviderError(
                f"HTTP {exc.code} {detail} (url={url})",
                status_code=exc.code, url=url, body=body, kind="http",
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(
                _redact(f"网络连接失败: {reason} (url={url})", api_key),
                url=url, kind="network",
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            if attempt < attempts - 1:
                time.sleep(3.0 * (attempt + 1))
                continue
            raise ProviderError(
                f"请求超时（{timeout:.0f}s） (url={url})", url=url, kind="timeout"
            ) from exc
        except OSError as exc:
            raise ProviderError(
                _redact(f"网络错误: {exc} (url={url})", api_key), url=url, kind="network"
            ) from exc
    raise ProviderError(
        f"HTTP 请求重试失败 (url={url})", url=url, kind="network")


def _extract_error_message(body: str) -> str:
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        if isinstance(error, str):
            return error[:300]
        for key in ("message", "msg", "detail"):
            if data.get(key):
                return str(data[key])[:300]
    return body[:300]


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #
def list_models(base_url: str, api_key: str, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """GET /models，返回模型 id 列表（保持服务端顺序，去重）。"""
    base = normalize_base_url(base_url)
    url = f"{base}/models"
    data = _request_json("GET", url, api_key, timeout=timeout)
    entries: Any = data.get("data") if isinstance(data, dict) else data
    models: list[str] = []
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
            else:
                model_id = item
            if isinstance(model_id, str) and model_id.strip():
                model_id = model_id.strip()
                if model_id not in models:
                    models.append(model_id)
    if not models:
        raise ProviderError(
            f"/models 响应中未找到模型列表: {json.dumps(data, ensure_ascii=False)[:400]}",
            url=url, kind="protocol",
        )
    return models


def chat(base_url: str, api_key: str, model: str,
         messages: list[dict[str, Any]],
         tools: list[dict[str, Any]] | None = None,
         temperature: float = 0.3,
         max_tokens: int | None = None,
         timeout: float = DEFAULT_TIMEOUT,
         tool_choice: str | dict[str, Any] | None = "auto",
         response_format: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST /chat/completions，返回归一化后的响应。

    返回 dict 结构::

        {
          "id": str, "model": str, "content": str,
          "tool_calls": [{"id": str, "name": str,
                          "arguments": dict | None, "arguments_raw": str}],
          "finish_reason": str, "usage": dict, "raw": dict
        }
    """
    if not isinstance(messages, list) or not messages:
        raise ProviderError("messages 不能为空", kind="config")
    if not model:
        raise ProviderError("model 不能为空", kind="config")
    base = normalize_base_url(base_url)
    # Kimi coding endpoint only accepts temperature=1.0 (HTTP 400 otherwise).
    if "api.kimi.com/coding" in base.lower():
        temperature = 1.0
    url = f"{base}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if response_format:
        payload["response_format"] = response_format

    data = _request_json("POST", url, api_key, payload=payload, timeout=timeout)
    return _normalize_chat_response(data, url, api_key)


def _normalize_chat_response(data: Any, url: str, api_key: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProviderError(f"chat 响应不是 JSON 对象 (url={url})", url=url,
                            body=str(data)[:500], kind="protocol")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(
            _redact(f"chat 响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:400]}",
                    api_key),
            url=url, kind="protocol",
        )
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):  # 少数服务商返回分段 content
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    content = (content or "").strip() if isinstance(content, str) else ""

    tool_calls: list[dict[str, Any]] = []
    raw_calls = message.get("tool_calls") or []
    if isinstance(raw_calls, list):
        for index, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or "").strip()
            arguments_raw = function.get("arguments")
            if isinstance(arguments_raw, dict):
                arguments = arguments_raw
                arguments_raw = json.dumps(arguments_raw, ensure_ascii=False)
            else:
                arguments_raw = str(arguments_raw or "")
                arguments = None
                if arguments_raw.strip():
                    try:
                        parsed = json.loads(arguments_raw)
                        if isinstance(parsed, dict):
                            arguments = parsed
                    except json.JSONDecodeError:
                        arguments = None
            call_id = str(call.get("id") or f"call_{index}")
            if name:
                tool_calls.append({
                    "id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "arguments_raw": arguments_raw,
                })

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "id": str(data.get("id") or ""),
        "model": str(data.get("model") or ""),
        "content": content,
        "tool_calls": tool_calls,
        "finish_reason": str(choice.get("finish_reason") or ""),
        "usage": usage,
        "raw": data,
    }


def list_models_or_default(base_url: str, api_key: str,
                           fallback: list[str] | tuple[str, ...] = (),
                           timeout: float = DEFAULT_TIMEOUT) -> tuple[list[str], str]:
    """获取模型清单：先请求 ``/models``，失败时回退内置清单。

    智谱 GLM 的 ``/models`` 未官方文档化，可能 404/401；此时必须回退
    ``PROVIDER_PRESETS`` 的 ``builtin_models``，保证设置界面仍可选用模型。

    Returns
    -------
    (models, source) — source 为 ``"remote"`` 或 ``"builtin"``。
    """
    try:
        models = list_models(base_url, api_key, timeout=timeout)
        if models:
            return models, "remote"
    except ProviderError as exc:
        if not fallback:
            raise
        return [str(item) for item in fallback], f"builtin（/models 失败: {exc.kind}）"
    return [str(item) for item in fallback], "builtin"


def make_tool_message(tool_call_id: str, result: Any) -> dict[str, Any]:
    """把工具执行结果包装成 OpenAI tool 消息。"""
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, default=str)
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result,
    }


def make_assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    """把归一化响应还原成可回传给服务端的 assistant 消息。"""
    message: dict[str, Any] = {"role": "assistant"}
    content = response.get("content") or ""
    tool_calls = response.get("tool_calls") or []
    message["content"] = content or None
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": call.get("arguments_raw")
                    or json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                },
            }
            for call in tool_calls
        ]
    return message
