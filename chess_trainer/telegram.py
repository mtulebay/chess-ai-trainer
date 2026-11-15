"""Отправка сообщений в Telegram через Bot API."""
from __future__ import annotations

import time

import requests

LIMIT = 4000  # у Telegram предел 4096 символов


def split(text: str, limit: int = LIMIT) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts


def send(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        print("[telegram] токен/чат не заданы — печатаю в stdout:\n" + text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split(text):
        for attempt in range(4):
            r = requests.post(url, json={"chat_id": chat_id, "text": chunk,
                                         "disable_web_page_preview": True}, timeout=30)
            if r.status_code == 429 and attempt < 3:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3))
                continue
            r.raise_for_status()
            break
