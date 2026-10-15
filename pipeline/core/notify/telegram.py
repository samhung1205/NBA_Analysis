"""
Telegram Bot API 傳送（notify-v1）
------------------------------------------------------------
只用 sendMessage（HTML parse mode）。token 只出現在請求 URL：requests 的例外訊息會帶完整 URL，
所以這裡一律只回報例外類別 / HTTP 狀態 / Telegram 的 description，**不把 token 寫進 log 或 DB**。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4096            # Telegram 單則訊息上限（字元）
TIMEOUT_S = 10


class TelegramError(RuntimeError):
    """送出失敗；訊息內容保證不含 token。"""


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """依行切成不超過 limit 的多則；單行過長則硬切。"""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks or [""]


@dataclass(frozen=True)
class TelegramClient:
    token: str
    chat_id: str
    post: Callable[..., requests.Response] = requests.post

    def send(self, text: str) -> int:
        """送出（必要時拆成多則）；回傳送出則數。任一則失敗 → TelegramError。"""
        n = 0
        for part in split_message(text):
            try:
                r = self.post(API.format(token=self.token),
                              json={"chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                                    "disable_web_page_preview": True},
                              timeout=TIMEOUT_S)
            except requests.RequestException as e:
                raise TelegramError(f"network: {type(e).__name__}") from None
            if r.status_code != 200:
                try:
                    desc = (r.json() or {}).get("description") or ""
                except ValueError:
                    desc = ""
                raise TelegramError(f"HTTP {r.status_code}: {desc[:200]}".replace(self.token, "***"))
            n += 1
        return n


def client_from_settings(settings) -> TelegramClient | None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return None
    return TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
