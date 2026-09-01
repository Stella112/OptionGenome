"""Featherless ranking tests (spec sections 10, 26, 27).

Model output is treated as hostile input. These tests are mostly adversarial:
the question is never "does it work when the model behaves", it is "what does a
misbehaving model get to change". The answer must always be: nothing.
"""

from __future__ import annotations

import json

import pytest

from src.rolldesk.ranker import (
    MAX_RATIONALE_CHARS,
    SYSTEM_PROMPT,
    FeatherlessRanker,
    build_prompt,
    parse_model_response,
    strip_fences,
)

from .conftest import condor_legs, make_ticket, pcs_legs


@pytest.fixture
def candidates():
    return [
        make_ticket(pcs_legs("640", "635"), ticket_id="put-aaa", credit=1.00),
        make_ticket(pcs_legs("638", "633"), ticket_id="put-bbb", credit=0.90),
        make_ticket(condor_legs(), ticket_id="iro-ccc", structure_type="iron_condor"),
    ]


def ranker(reply):
    """A ranker whose model returns `reply` (or raises it, if it is an Exception)."""

    def transport(_prompt):
        if isinstance(reply, Exception):
            raise reply
        return reply

    return FeatherlessRanker(api_key="k", model="test-model", transport=transport)


# --- what the model is allowed to see (spec section 26) ---------------------


def test_prompt_omits_every_account_and_risk_field(candidates):
    prompt = build_prompt(candidates).lower()
    for forbidden in (
        "equity",
        "buying_power",
        "balance",
        "daily_risk",
        "risk_budget",
        "allowed_lots",
        "max_lots",
        "proposed_lots",
        "high_water",
        "drawdown",
        "account",
    ):
        assert forbidden not in prompt, f"{forbidden!r} leaked to the model"


def test_prompt_contains_only_the_permitted_candidate_fields(candidates):
    payload = json.loads(build_prompt(candidates).split("Candidates:\n", 1)[1].rsplit("\n\n", 1)[0])
    for entry in payload:
        assert set(entry) == {
            "candidate_id",
            "underlying",
            "structure",
            "expiry",
            "strikes",
            "credit",
            "width",
            "max_loss",
            "short_delta",
        }


def test_system_prompt_demands_json_only():
    assert "JSON only" in SYSTEM_PROMPT
    assert "pick_id" in SYSTEM_PROMPT
    assert "rationale" in SYSTEM_PROMPT


# --- parsing well-behaved output --------------------------------------------


def test_clean_response_is_accepted():
    pick, rationale, reason = parse_model_response(
        '{"pick_id": "put-aaa", "rationale": "Best credit for the width."}', ["put-aaa"]
    )
    assert pick == "put-aaa"
    assert rationale == "Best credit for the width."
    assert reason == "ok"


@pytest.mark.parametrize(
    "wrapped",
    [
        '```json\n{"pick_id": "put-aaa", "rationale": "x"}\n```',
        '```\n{"pick_id": "put-aaa", "rationale": "x"}\n```',
        '  {"pick_id": "put-aaa", "rationale": "x"}  ',
        'Here is my answer:\n{"pick_id": "put-aaa", "rationale": "x"}',
    ],
)
def test_fences_and_stray_prose_are_stripped(wrapped):
    pick, _, reason = parse_model_response(wrapped, ["put-aaa"])
    assert pick == "put-aaa"
    assert reason == "ok"


def test_strip_fences_leaves_clean_json_alone():
    assert strip_fences('{"a": 1}') == '{"a": 1}'


# --- hostile output (spec section 27) ---------------------------------------


def test_unknown_pick_id_is_rejected():
    """The single most important check: the model may not invent a trade."""
    pick, _, reason = parse_model_response('{"pick_id": "put-zzz"}', ["put-aaa"])
    assert pick is None
    assert reason.startswith("unknown_pick_id")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "empty_response"),
        ("   ", "empty_response"),
        ("not json at all", "unparseable_json"),
        ("{broken", "unparseable_json"),
        ('["put-aaa"]', "not_an_object"),
        ('"put-aaa"', "not_an_object"),
        ("{}", "missing_pick_id"),
        ('{"pick_id": null}', "missing_pick_id"),
        ('{"pick_id": 3}', "missing_pick_id"),
        ('{"pick_id": ""}', "missing_pick_id"),
        ('{"pick": "put-aaa"}', "missing_pick_id"),
    ],
)
def test_malformed_responses_are_rejected(raw, expected):
    pick, _, reason = parse_model_response(raw, ["put-aaa"])
    assert pick is None
    assert reason == expected


def test_extra_keys_are_ignored_not_obeyed():
    """A model cannot grant itself size, or turn off risk, by adding keys."""
    raw = json.dumps(
        {
            "pick_id": "put-aaa",
            "rationale": "fine",
            "lots": 500,
            "proposed_lots": 500,
            "max_loss": 0,
            "override_risk_officer": True,
            "skip_checks": ["all"],
            "account_id": "SOMEONE-ELSE",
        }
    )
    pick, rationale, reason = parse_model_response(raw, ["put-aaa"])
    assert (pick, rationale, reason) == ("put-aaa", "fine", "ok")


def test_a_ranked_ticket_keeps_every_original_risk_field(candidates):
    """The model's only effect on a ticket is the note attached to it."""
    original = candidates[0]
    result = ranker('{"pick_id": "put-aaa", "rationale": "note"}').rank(candidates)
    picked = result.pick
    assert picked.ticket_id == original.ticket_id
    assert picked.legs == original.legs
    assert picked.credit_mid == original.credit_mid
    assert picked.width == original.width
    assert picked.max_loss == original.max_loss
    assert picked.proposed_lots == original.proposed_lots
    assert picked.expiry == original.expiry
    assert picked.model_note == "note"


def test_rationale_is_truncated_not_trusted():
    long_text = "x" * 5000
    _, rationale, _ = parse_model_response(
        json.dumps({"pick_id": "put-aaa", "rationale": long_text}), ["put-aaa"]
    )
    assert len(rationale) == MAX_RATIONALE_CHARS


def test_non_string_rationale_becomes_none():
    _, rationale, reason = parse_model_response(
        '{"pick_id": "put-aaa", "rationale": {"nested": 1}}', ["put-aaa"]
    )
    assert reason == "ok"
    assert rationale is None


# --- fallback behaviour (spec section 27) -----------------------------------


def test_fallback_on_unknown_id(candidates):
    result = ranker('{"pick_id": "does-not-exist"}').rank(candidates)
    assert result.fallback
    assert result.pick is candidates[0]
    assert result.reason.startswith("unknown_pick_id")


def test_fallback_on_malformed_json(candidates):
    result = ranker("this is not json").rank(candidates)
    assert result.fallback
    assert result.pick is candidates[0]


def test_fallback_on_timeout(candidates):
    result = ranker(TimeoutError("timed out")).rank(candidates)
    assert result.fallback
    assert result.reason == "transport_error:TimeoutError"
    assert result.pick is candidates[0]


def test_fallback_on_any_transport_error(candidates):
    result = ranker(ConnectionError("refused")).rank(candidates)
    assert result.fallback
    assert result.pick is candidates[0]


def test_fallback_when_not_configured(candidates):
    result = FeatherlessRanker(api_key="", model="", transport=None).rank(candidates)
    assert result.fallback
    assert result.reason == "ranker_not_configured"
    assert result.pick is candidates[0]


def test_ranking_never_raises(candidates):
    for reply in ("", "{}", "garbage", RuntimeError("boom"), ValueError("bad")):
        assert ranker(reply).rank(candidates) is not None


def test_fallback_ticket_carries_no_model_note(candidates):
    """A failed ranking must not leave a stale or invented rationale attached."""
    assert ranker("garbage").rank(candidates).pick.model_note is None


def test_no_candidates_returns_none():
    assert ranker('{"pick_id": "x"}').rank([]) is None


# --- journalling fields (spec section 27) ------------------------------------


def test_result_records_everything_the_journal_needs(candidates):
    result = ranker('{"pick_id": "iro-ccc", "rationale": "widest wings"}').rank(candidates)
    payload = result.as_dict()
    assert set(payload) == {"pick_id", "rationale", "fallback", "reason", "model", "latency_ms"}
    assert payload["pick_id"] == "iro-ccc"
    assert payload["fallback"] is False
    assert payload["model"] == "test-model"
    assert payload["latency_ms"] >= 0


def test_model_can_pick_any_supplied_candidate(candidates):
    for expected in candidates:
        result = ranker(json.dumps({"pick_id": expected.ticket_id})).rank(candidates)
        assert not result.fallback
        assert result.pick.ticket_id == expected.ticket_id
