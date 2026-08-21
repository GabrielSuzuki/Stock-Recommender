"""
Telegram delivery for the stock recommender.

Design goals:
  - Never raise into the caller. A broken notifier must not kill the pipeline;
    it reports failure and returns False.
  - Always deliver something. Silence is indistinguishable from a crashed job,
    so RISK_OFF days and empty screens still get a message.
  - Respect Telegram's limits rather than discovering them at 5 AM.

Telegram limits enforced here:
  - sendMessage text .......... 4096 chars
  - sendPhoto caption ......... 1024 chars
  - per-chat send rate ........ ~1 message/second (we pace at 1.05s)
  - HTTP 429 responses carry parameters.retry_after (seconds) which we honor
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
PER_CHAT_INTERVAL = 1.05          # seconds between sends to one chat
MAX_ATTEMPTS = 4
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 20

# Characters Telegram requires be backslash-escaped in MarkdownV2 parse mode.
# Missing even one of these produces a 400 "can't parse entities" and the
# message is silently not delivered.
_MDV2_RESERVED = r"_*[]()~`>#+-=|{}.!"


def escape_md(text: str) -> str:
    """Escape every MarkdownV2 reserved character in `text`.

    Apply this to any *dynamic* content (tickers, prices, headlines) before
    interpolating it into a formatted message. Do NOT apply it to the markup
    you are deliberately writing, or your bold markers become literal asterisks.
    """
    out = []
    for ch in str(text):
        if ch in _MDV2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def chunk_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split `text` into pieces under `limit`, preferring clean break points.

    Tries paragraph breaks, then line breaks, then a hard cut. Never splits in
    a way that leaves a trailing backslash, which would escape the newline we
    add and corrupt the next chunk's parsing.
    """
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit

        # Never end a chunk on an odd number of trailing backslashes.
        while cut > 0 and (len(remaining[:cut]) - len(remaining[:cut].rstrip("\\"))) % 2 == 1:
            cut -= 1

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    alert_chat_id: str | None = None   # optional separate channel for failures
    parse_mode: str = "MarkdownV2"
    disable_preview: bool = True

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set. "
                "See docs/setup-telegram.md."
            )
        return cls(
            bot_token=token,
            chat_id=chat,
            alert_chat_id=os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip() or None,
        )


class Telegram:
    def __init__(self, config: TelegramConfig | None = None, session=None):
        self.cfg = config or TelegramConfig.from_env()
        self.session = session or requests.Session()
        self._last_send = 0.0

    # ---------------------------------------------------------------- internals

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.cfg.bot_token}/{method}"

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if elapsed < PER_CHAT_INTERVAL:
            time.sleep(PER_CHAT_INTERVAL - elapsed)

    def _post(self, method: str, data: dict, files: dict | None = None) -> bool:
        """POST with backoff. Returns True on success, False after exhausting retries."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._pace()
            try:
                resp = self.session.post(
                    self._url(method),
                    data=data,
                    files=files,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except requests.RequestException as exc:
                wait = min(2 ** attempt, 30)
                log.warning("telegram %s network error (attempt %d): %s", method, attempt, exc)
                time.sleep(wait)
                continue
            finally:
                self._last_send = time.monotonic()

            if resp.status_code == 200:
                return True

            if resp.status_code == 429:
                body = _safe_json(resp)
                wait = int(body.get("parameters", {}).get("retry_after", 5)) + 1
                log.warning("telegram rate limited, sleeping %ss", wait)
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                wait = min(2 ** attempt, 30)
                log.warning("telegram %s server error %s, retrying in %ss",
                            method, resp.status_code, wait)
                time.sleep(wait)
                continue

            # 400-class: retrying will not help. Most commonly a MarkdownV2
            # escaping bug. Log the body so it is diagnosable after the fact.
            log.error("telegram %s failed %s: %s", method, resp.status_code, resp.text[:500])
            return False

        log.error("telegram %s gave up after %d attempts", method, MAX_ATTEMPTS)
        return False

    # ------------------------------------------------------------------- public

    def send(self, text: str, chat_id: str | None = None, parse_mode: str | None = None) -> bool:
        """Send text, splitting automatically if over the 4096-char limit."""
        chat = chat_id or self.cfg.chat_id
        mode = self.cfg.parse_mode if parse_mode is None else parse_mode
        pieces = chunk_text(text)
        if not pieces:
            return True

        ok = True
        for i, piece in enumerate(pieces):
            payload = {
                "chat_id": chat,
                "text": piece,
                "disable_web_page_preview": self.cfg.disable_preview,
            }
            if mode:
                payload["parse_mode"] = mode
            sent = self._post("sendMessage", dict(payload))
            if not sent and mode:
                # Fall back to plain text so the content still arrives even if
                # the formatting is malformed. Delivery beats prettiness.
                # Build a fresh payload rather than mutating the one already
                # handed to the transport.
                log.warning("retrying chunk %d as plain text", i)
                fallback = {k: v for k, v in payload.items() if k != "parse_mode"}
                sent = self._post("sendMessage", fallback)
            ok = ok and sent
        return ok

    def send_photo(self, image_path: str | Path, caption: str = "",
                   chat_id: str | None = None) -> bool:
        """Send an image with an optional caption (truncated to 1024 chars)."""
        path = Path(image_path)
        if not path.is_file():
            log.error("send_photo: no such file %s", path)
            return False
        if len(caption) > CAPTION_LIMIT:
            caption = caption[: CAPTION_LIMIT - 1] + "…"
        with path.open("rb") as fh:
            return self._post(
                "sendPhoto",
                {
                    "chat_id": chat_id or self.cfg.chat_id,
                    "caption": caption,
                    "parse_mode": self.cfg.parse_mode,
                },
                files={"photo": fh},
            )

    def alert(self, message: str) -> bool:
        """Failure path. Plain text, no parse mode, goes to the alert chat if set.

        Deliberately avoids MarkdownV2: an alert about a crash must not itself
        fail to send because a stack trace contained an underscore.
        """
        return self.send(
            f"⚠️ {message}",
            chat_id=self.cfg.alert_chat_id or self.cfg.chat_id,
            parse_mode="",
        )

    def heartbeat(self, stage: str, detail: str = "") -> bool:
        body = f"✓ {stage} completed"
        if detail:
            body += f" — {detail}"
        return self.send(body, parse_mode="")

    def get_updates(self, offset: int | None = None) -> list[dict]:
        """Poll for incoming messages. Used by the reply-to-log journal feature."""
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self.session.get(self._url("getUpdates"), params=params,
                                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            resp.raise_for_status()
            return resp.json().get("result", [])
        except (requests.RequestException, ValueError) as exc:
            log.error("get_updates failed: %s", exc)
            return []


def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


# --------------------------------------------------------------- brief builder

def build_brief(regime: str, regime_note: str, picks: Sequence[dict],
                as_of: str) -> str:
    """Render the morning brief in MarkdownV2.

    `picks` items are expected to carry: ticker, thesis, entry, stop, target,
    rr, shares, invalidation. All dynamic values are escaped; the markup is not.
    """
    e = escape_md
    icon = {"RISK_ON": "\U0001F7E2", "NEUTRAL": "\U0001F7E1", "RISK_OFF": "\U0001F534"}
    head = f"*Morning Brief — {e(as_of)}*\n{icon.get(regime, '')} *{e(regime)}* — {e(regime_note)}"

    if regime == "RISK_OFF" or not picks:
        reason = "regime is risk\\-off" if regime == "RISK_OFF" else "no setups passed the screen"
        return f"{head}\n\n_No new entries today — {reason}\\._"

    lines = [head, ""]
    for p in picks:
        lines.append(
            f"*{e(p['ticker'])}*  •  entry {e(p['entry'])}  •  stop {e(p['stop'])}\n"
            f"{e(p['thesis'])}\n"
            f"target {e(p['target'])}  ·  R:R {e(p['rr'])}  ·  size {e(p['shares'])} sh\n"
            f"_invalid if_ {e(p['invalidation'])}"
        )
        lines.append("")
    lines.append("_Reply_ `TOOK TICKER QTY @ PRICE` _to log a fill\\._")
    return "\n".join(lines)
