"""Featherless candidate ranking (spec sections 10, 26, 27).

The model is UNTRUSTED. It receives immutable candidate descriptions and may
return exactly one thing: the id of a candidate it was given, plus a one-line
rationale. It cannot create or change a strike, expiry, size, credit or limit,
cannot call a broker tool, and cannot override the Risk Officer.

Everything the model returns is treated as hostile input. Any failure -- bad
JSON, unknown id, wrong shape, timeout -- falls back to candidates[0] and is
journaled with fallback=true. The desk never stalls waiting on a model, and
never acts on one.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Sequence

from ..types import Ticket

DEFAULT_BASE_URL = "https://api.featherless.ai/v1"
REQUEST_TIMEOUT_SECONDS = 20.0

#: Hard cap on a rationale. A model that returns an essay gets truncated, not obeyed.
MAX_RATIONALE_CHARS = 280

SYSTEM_PROMPT = """You rank pre-validated options trade candidates.

Return JSON only.
No prose.
No markdown fences.
No additional keys.

Output exactly:
{
  "pick_id": "<one supplied candidate ID>",
  "rationale": "<one sentence>"
}"""

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class RankResult:
    """The outcome of a ranking attempt. Always usable, never None."""

    pick: Ticket
    rationale: str | None
    fallback: bool
    reason: str
    model: str
    latency_ms: float

    def as_dict(self) -> dict:
        return {
            "pick_id": self.pick.ticket_id,
            "rationale": self.rationale,
            "fallback": self.fallback,
            "reason": self.reason,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
        }


def strip_fences(text: str) -> str:
    """Remove markdown fences a model added despite being told not to."""
    return _FENCE_RE.sub("", text or "").strip()


def parse_model_response(raw: str, valid_ids: Sequence[str]) -> tuple[str | None, str | None, str]:
    """Parse hostile model output.

    Returns (pick_id, rationale, reason). pick_id is None whenever the response
    cannot be trusted, and `reason` always explains why.

    Every field other than pick_id and rationale is ignored -- a model that adds
    "lots": 50 or "override_risk": true is simply not listened to.
    """
    if not raw or not raw.strip():
        return None, None, "empty_response"

    text = strip_fences(raw)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # A model may wrap the object in stray prose. Take the first object and
        # try once more; anything less parseable than that is rejected.
        match = _OBJECT_RE.search(text)
        if not match:
            return None, None, "unparseable_json"
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None, None, "unparseable_json"

    if not isinstance(payload, dict):
        return None, None, "not_an_object"

    pick_id = payload.get("pick_id")
    if not isinstance(pick_id, str) or not pick_id.strip():
        return None, None, "missing_pick_id"
    pick_id = pick_id.strip()

    if pick_id not in valid_ids:
        # The single most important check: the model may only choose from what
        # it was given. It cannot invent a trade.
        return None, None, f"unknown_pick_id:{pick_id[:40]}"

    rationale = payload.get("rationale")
    if isinstance(rationale, str):
        rationale = rationale.strip()[:MAX_RATIONALE_CHARS] or None
    else:
        rationale = None

    return pick_id, rationale, "ok"


def build_prompt(candidates: Sequence[Ticket]) -> str:
    """Serialise candidates for the model.

    Uses Ticket.for_ranking(), which deliberately omits account balance, buying
    power, max account risk, daily risk budget, allowed lots, and every Risk
    Officer rule (spec section 26). The model does not need them.
    """
    payload = [t.for_ranking() for t in candidates]
    return (
        "Candidates:\n"
        + json.dumps(payload, indent=2)
        + "\n\nReturn the JSON object described in the system prompt."
    )


class FeatherlessRanker:
    """Calls Featherless, or degrades silently to the deterministic fallback."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        transport=None,
    ):
        self.api_key = api_key if api_key is not None else os.getenv("FEATHERLESS_API_KEY", "")
        self.model = model or os.getenv("FEATHERLESS_MODEL", "")
        self.base_url = (base_url or os.getenv("FEATHERLESS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        #: Injected for tests: a callable(prompt) -> raw string.
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    def _call(self, prompt: str) -> str:
        if self._transport is not None:
            return self._transport(prompt)

        import httpx

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 200,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def rank(self, candidates: Sequence[Ticket]) -> RankResult | None:
        """Choose one candidate. Returns None only when there is nothing to choose.

        Never raises. Every failure path lands on candidates[0] with fallback=True.
        """
        if not candidates:
            return None

        default = candidates[0]
        valid_ids = [t.ticket_id for t in candidates]

        if not self.configured:
            return RankResult(
                pick=default,
                rationale=None,
                fallback=True,
                reason="ranker_not_configured",
                model=self.model or "none",
                latency_ms=0.0,
            )

        started = time.monotonic()
        try:
            raw = self._call(build_prompt(candidates))
        except Exception as exc:
            return RankResult(
                pick=default,
                rationale=None,
                fallback=True,
                reason=f"transport_error:{type(exc).__name__}",
                model=self.model,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        latency_ms = (time.monotonic() - started) * 1000

        pick_id, rationale, reason = parse_model_response(raw, valid_ids)
        if pick_id is None:
            return RankResult(
                pick=default,
                rationale=None,
                fallback=True,
                reason=reason,
                model=self.model,
                latency_ms=latency_ms,
            )

        pick = next(t for t in candidates if t.ticket_id == pick_id)
        # The rationale rides along as METADATA only. Attaching it is the single
        # mutation the ranking layer may cause, and it changes no risk field.
        return RankResult(
            pick=pick.with_model_note(rationale),
            rationale=rationale,
            fallback=False,
            reason="ok",
            model=self.model,
            latency_ms=latency_ms,
        )
