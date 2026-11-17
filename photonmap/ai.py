"""用视觉模型给照片猜地名（可选功能）。

模型只出**地名**，不出坐标——模型给坐标一定是编的，
地名再交给 geocode 反查成坐标，与人工标定走同一条落地链路。

没装 anthropic 库、或者没有可用的凭据时，整个功能降级，
其余功能不受影响（核心依赖里没有 anthropic）。
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger(__name__)

# 模型可以用环境变量换掉
MODEL = os.environ.get("PHOTONMAP_AI_MODEL", "claude-opus-5-5")

# 一次只要几个地名，输出很短
MAX_TOKENS = 4096

PROMPT = """这是一张旅行照片。请根据画面里能看出来的线索——建筑、路牌、店铺招牌、
文字、地标、风景特征——列出这张照片最可能拍在什么地方。

要求：
1. 只给地名（景点名、建筑名、街道名、店铺名、城市名），不要给经纬度；
2. 最多给 3 个，按可能性从高到低排列；
3. 只根据画面里**看得见**的线索判断。画面是纯色、糊的、看不出任何可辨认的
   景物时，必须返回空数组 []——宁可说看不出来，也不要编地名；
4. 用中文输出，严格是 JSON 数组，例如 ["断桥残雪", "杭州西湖", "浙江省杭州市"]。"""

_client = None
_client_checked = False


class AIUnavailable(Exception):
    """没装 SDK 或没有凭据。"""


def available() -> tuple[bool, str]:
    """能不能用 AI 识别。返回 (可用, 不可用的原因)。"""
    global _client, _client_checked
    if _client_checked and _client is not None:
        return True, ""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "没装 anthropic 库（pip install 'photonmap[ai]'）"
    try:
        _client = anthropic.Anthropic()
    except Exception as exc:  # 没配 ANTHROPIC_API_KEY 等
        _client = None
        return False, f"没有可用的 Anthropic 凭据：{exc}"
    _client_checked = True
    return True, ""


def _text_of(message) -> str:
    """把响应里的文本块拼起来。"""
    parts = []
    for block in message.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "".join(parts)


def _parse_names(text: str) -> list[str]:
    """从模型输出里挑出地名数组。

    模型有时会给 JSON 加代码围栏或前后废话，这里都容得下。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\[.*?\]", text, re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return []
    if not isinstance(data, list):
        return []
    names = []
    for item in data:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names[:3]


def suggest_places(jpeg_bytes: bytes, context: str = "") -> list[str]:
    """给一张照片猜地名，返回最多 3 个。

    jpeg_bytes 是照片的 JPEG 编码（服务端转好的）。
    context 是可选的补充信息（比如拍摄时间、文件名）。
    """
    ok, reason = available()
    if not ok:
        raise AIUnavailable(reason)

    import anthropic

    import base64

    payload = base64.standard_b64encode(jpeg_bytes).decode("ascii")
    text = PROMPT
    if context:
        text += f"\n\n补充信息：{context}"

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": payload,
                    },
                },
                {"type": "text", "text": text},
            ],
        }
    ]

    # 出请求时带上服务端的回退机制（按拒答类别自动换模型），
    # 万一被安全分类器拒了不至于整个功能不可用。
    # 账号没开这个 beta 时退回普通请求。
    try:
        message = _client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            messages=messages,
        )
    except anthropic.BadRequestError as exc:
        log.info("回退参数不被支持，改用普通请求：%s", exc)
        message = _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            output_config={"effort": "low"},
            messages=messages,
        )

    if getattr(message, "stop_reason", "") == "refusal":
        raise RuntimeError("模型拒绝了这个请求")
    return _parse_names(_text_of(message))
