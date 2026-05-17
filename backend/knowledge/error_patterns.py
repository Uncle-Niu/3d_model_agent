"""
Autonomous error-pattern learning.

The orchestrator records every CadQuery failure and the fix that was applied
to ``log.jsonl`` (LRU-capped). After a turn that involved at least one repair
AND ended successfully, an LLM summarizer condenses the journey into one or
more *pitfall cards* — short, generic rules ("when X happens, do Y") stored
in ``pitfalls.json``. The top-K cards (by frequency × recency) get folded
into the next turn's system prompt under "Learned Pitfalls".

Design notes
------------
- Recording is synchronous and cheap (single file append). The orchestrator
  must not block on it.
- Summarization is async and fire-and-forget — it does *not* block the user's
  response. The same local LLM that wrote the code is reused so we don't add
  another model dependency.
- LRU policy: log is capped at ~1000 lines, pitfall cards at ~25. Frequency
  bumps + last-used timestamps shape eviction.
- A pitfall card has a ``trigger_hint`` — a short substring used to match new
  log entries against existing cards (so a recurring error increments
  frequency rather than spawning duplicates).
- We never modify the system prompt file on disk. Cards are injected at
  prompt-build time, so disabling the feature is a one-line revert.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_DEFAULT_ROOT = Path("data") / "error_patterns"
_LOG_FILENAME = "log.jsonl"
_CARDS_FILENAME = "pitfalls.json"

# LRU caps. The log records every failure; cards are the distilled lessons.
LOG_MAX_LINES = 1000
CARDS_MAX = 25

# How many learned pitfall cards to inject into the system prompt at most.
PROMPT_INJECTION_LIMIT = 8

# Minimum count of repairs in a turn before we bother asking the LLM to
# summarize. Was 1; bumped to 2 because the actually-painful turns burn 3-8
# repair iterations, while single-repair turns rarely surface a lesson
# distinct from what the system prompt already carries — and the summarizer
# LLM call costs ~30s of latency for low signal-to-noise output. Failed
# turns are increasingly the source of useful lessons (see
# `turn_succeeded` plumbing) so we want the threshold to gate on real
# struggle, not occasional hiccups.
MIN_REPAIRS_FOR_SUMMARY = 2

# Process-wide lock so concurrent pipeline runs don't corrupt the JSON files.
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FailureEvent:
    """One record in ``log.jsonl``.

    ``error_signature`` is a normalized short string used both for de-duping
    in the log and as a candidate trigger_hint when summarizing.
    """
    timestamp: str
    failure_type: str               # syntax_error, execution_error, geometry_invalid, ...
    error_first_line: str           # first non-empty line of the traceback
    error_signature: str            # normalized lowercase short tag
    fix_kind: str                   # "mechanical" | "llm" | "none" | "vision"
    succeeded: bool                 # did the next iteration's execution pass?
    iteration: int = 0
    turn_id: str = ""               # opaque id for the conversation turn
    model: str = ""                 # which LLM was generating the code

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PitfallCard:
    """One distilled lesson.

    The ``rule`` is what gets folded into the prompt. ``trigger_hint`` is a
    short substring searched against new errors to decide whether a new log
    entry reinforces an existing card or warrants a new one.
    """
    card_id: str
    rule: str
    trigger_hint: str
    frequency: int = 1
    last_used: str = ""            # ISO timestamp — when this card last matched a new error
    created_at: str = ""           # ISO timestamp — first observed
    source_event_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _root() -> Path:
    """Root directory for the pattern store. Configurable via env for tests."""
    override = os.environ.get("ERROR_PATTERNS_DIR")
    return Path(override) if override else _DEFAULT_ROOT


def _log_path() -> Path:
    return _root() / _LOG_FILENAME


def _cards_path() -> Path:
    return _root() / _CARDS_FILENAME


def _ensure_root() -> None:
    _root().mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEX_RE = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_QUOTED_RE = re.compile(r"['\"][^'\"]{1,80}['\"]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_error(error_text: str) -> str:
    """Reduce an error message to a short, comparable signature.

    Drops line numbers, hex addresses, and quoted user values so that
    ``"line 12: NameError: name 'foo' is not defined"`` and
    ``"line 47: NameError: name 'bar' is not defined"`` collapse to the
    same signature. The signature is what we hash for the card id.
    """
    if not error_text:
        return ""
    text = error_text.strip().splitlines()[0] if error_text.strip() else ""
    text = text[:300]
    text = _HEX_RE.sub("0xN", text)
    text = _NUM_RE.sub("N", text)
    text = _QUOTED_RE.sub("'X'", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().lower()


def _first_error_line(error_text: str) -> str:
    if not error_text:
        return ""
    for line in error_text.splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Log: append + LRU rotation
# ---------------------------------------------------------------------------

def record_failure(
    failure_type: str,
    error_text: str,
    fix_kind: str,
    succeeded: bool,
    *,
    iteration: int = 0,
    turn_id: str = "",
    model: str = "",
) -> Optional[FailureEvent]:
    """Append a failure event to ``log.jsonl``. Returns the event written, or
    ``None`` if the inputs were too sparse to be useful.

    Safe to call from any code path: I/O errors are logged but never raised,
    so a broken disk doesn't take the orchestrator down.
    """
    if not failure_type and not error_text:
        return None
    try:
        event = FailureEvent(
            timestamp=_now_iso(),
            failure_type=failure_type or "unknown",
            error_first_line=_first_error_line(error_text),
            error_signature=normalize_error(error_text),
            fix_kind=fix_kind or "none",
            succeeded=bool(succeeded),
            iteration=int(iteration),
            turn_id=turn_id or "",
            model=model or "",
        )
        with _lock:
            _ensure_root()
            with _log_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_json(), ensure_ascii=False))
                f.write("\n")
            _maybe_rotate_log()
        return event
    except Exception as exc:
        logger.warning("error_patterns: failed to record failure: %s", exc)
        return None


def update_failure_outcome(
    *,
    turn_id: str,
    iteration: int,
    fix_kind: Optional[str] = None,
    succeeded: Optional[bool] = None,
) -> bool:
    """Patch the most recent matching entry in ``log.jsonl`` to reflect what
    actually happened to that failure (which repair strategy fired and whether
    the next iteration's execution finally passed).

    Without this, every persisted event stays frozen at the placeholder
    ``fix_kind="pending"`` / ``succeeded=False`` it was first written with —
    even after the orchestrator mutates the in-memory event. That makes the
    log useless for any cross-turn analysis.

    Matches on ``(turn_id, iteration)`` and updates the LAST occurrence (in
    case the same turn re-entered with the same iteration number, which
    shouldn't happen but is cheap to defend against). Returns True if a
    row was patched, False otherwise. Never raises.
    """
    if not turn_id or fix_kind is None and succeeded is None:
        return False
    path = _log_path()
    if not path.exists():
        return False
    try:
        with _lock:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            patched = False
            # Walk from the end so we update the most recent matching entry.
            for idx in range(len(lines) - 1, -1, -1):
                raw = lines[idx].strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("turn_id") != turn_id:
                    continue
                if int(data.get("iteration", -1)) != int(iteration):
                    continue
                if fix_kind is not None:
                    data["fix_kind"] = fix_kind
                if succeeded is not None:
                    data["succeeded"] = bool(succeeded)
                lines[idx] = json.dumps(data, ensure_ascii=False) + "\n"
                patched = True
                break
            if patched:
                with path.open("w", encoding="utf-8") as f:
                    f.writelines(lines)
            return patched
    except Exception as exc:
        logger.warning("error_patterns: update_failure_outcome failed: %s", exc)
        return False


def _maybe_rotate_log() -> None:
    """Trim ``log.jsonl`` to the most recent ``LOG_MAX_LINES`` lines.

    Called opportunistically after appends. Cheap enough to do every time the
    file crosses 1.1× the cap.
    """
    path = _log_path()
    try:
        if not path.exists():
            return
        # Quick size gate — line counting is O(n).
        size_bytes = path.stat().st_size
        if size_bytes < 64 * LOG_MAX_LINES:  # ~64 bytes/line is a safe lower bound
            return
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= LOG_MAX_LINES:
            return
        keep = lines[-LOG_MAX_LINES:]
        with path.open("w", encoding="utf-8") as f:
            f.writelines(keep)
    except Exception as exc:
        logger.warning("error_patterns: log rotation failed: %s", exc)


def read_log(limit: Optional[int] = None) -> list[FailureEvent]:
    """Return events from the log (most recent first when limit is given)."""
    path = _log_path()
    if not path.exists():
        return []
    events: list[FailureEvent] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    events.append(FailureEvent(**data))
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("error_patterns: log read failed: %s", exc)
        return []
    if limit is not None and limit > 0:
        return events[-limit:][::-1]
    return events


# ---------------------------------------------------------------------------
# Pitfall cards — load/save
# ---------------------------------------------------------------------------

def load_pitfalls() -> list[PitfallCard]:
    path = _cards_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        cards = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                cards.append(PitfallCard(**entry))
            except TypeError:
                # Tolerate older schemas: fill missing fields with defaults.
                cards.append(PitfallCard(
                    card_id=str(entry.get("card_id") or hashlib.sha1(str(entry).encode()).hexdigest()[:12]),
                    rule=str(entry.get("rule", "")),
                    trigger_hint=str(entry.get("trigger_hint", "")),
                    frequency=int(entry.get("frequency", 1)),
                    last_used=str(entry.get("last_used", "")),
                    created_at=str(entry.get("created_at", "")),
                    source_event_count=int(entry.get("source_event_count", 0)),
                ))
        return cards
    except Exception as exc:
        logger.warning("error_patterns: pitfall load failed: %s", exc)
        return []


def save_pitfalls(cards: list[PitfallCard]) -> None:
    try:
        with _lock:
            _ensure_root()
            tmp = _cards_path().with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump([c.to_json() for c in cards], f, ensure_ascii=False, indent=2)
            tmp.replace(_cards_path())
    except Exception as exc:
        logger.warning("error_patterns: pitfall save failed: %s", exc)


# ---------------------------------------------------------------------------
# Card matching / merging
# ---------------------------------------------------------------------------

def _card_id_for(rule: str, trigger_hint: str) -> str:
    seed = (rule + "||" + trigger_hint).lower().strip()
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _find_matching_card(cards: list[PitfallCard], rule: str, trigger_hint: str) -> Optional[int]:
    """Return the index of an existing card that the new one duplicates.

    Match is based on identical ``card_id`` first, then on the trigger_hint
    being a substring of either the existing rule or hint (and vice versa).
    """
    if not rule:
        return None
    new_id = _card_id_for(rule, trigger_hint)
    for idx, card in enumerate(cards):
        if card.card_id == new_id:
            return idx
        # Soft match: trigger hint significantly overlaps
        if trigger_hint and card.trigger_hint:
            a, b = trigger_hint.lower(), card.trigger_hint.lower()
            if a == b:
                return idx
            if len(a) >= 8 and (a in b or b in a):
                return idx
    return None


def _merge_card(cards: list[PitfallCard], rule: str, trigger_hint: str, source_events: int) -> None:
    """Add a new card or merge it into an existing entry. Mutates ``cards``."""
    if not rule.strip():
        return
    rule = rule.strip()[:400]
    trigger_hint = trigger_hint.strip()[:120]
    idx = _find_matching_card(cards, rule, trigger_hint)
    now = _now_iso()
    if idx is not None:
        card = cards[idx]
        card.frequency += 1
        card.last_used = now
        card.source_event_count += max(0, source_events)
        # Prefer the longer/more specific rule text when the new wording is
        # richer than what we had. Avoids stale short stubs winning forever.
        if len(rule) > len(card.rule) and len(rule) <= 400:
            card.rule = rule
        return
    cards.append(PitfallCard(
        card_id=_card_id_for(rule, trigger_hint),
        rule=rule,
        trigger_hint=trigger_hint,
        frequency=1,
        last_used=now,
        created_at=now,
        source_event_count=max(0, source_events),
    ))


def _evict_to_cap(cards: list[PitfallCard], cap: int = CARDS_MAX) -> list[PitfallCard]:
    """LRU eviction: keep the highest score cards.

    Score = frequency × recency boost. Recency boost decays linearly with the
    age of ``last_used``. Avoids both "stale top-frequency card never leaves"
    and "single-shot recency card displaces everything useful".
    """
    if len(cards) <= cap:
        return cards
    now = datetime.now(timezone.utc)

    def score(card: PitfallCard) -> float:
        try:
            ts = datetime.fromisoformat(card.last_used) if card.last_used else now
        except ValueError:
            ts = now
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        recency = max(0.1, 1.0 - min(age_days, 30.0) / 30.0)
        return float(card.frequency) * recency

    cards.sort(key=score, reverse=True)
    return cards[:cap]


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def get_active_pitfalls(max_cards: int = PROMPT_INJECTION_LIMIT) -> list[PitfallCard]:
    """Return the top-N pitfall cards to inject into the system prompt."""
    cards = load_pitfalls()
    if not cards:
        return []
    cards = _evict_to_cap(cards, cap=max(max_cards, CARDS_MAX))
    cards.sort(key=lambda c: c.frequency, reverse=True)
    return cards[:max_cards]


def format_pitfalls_for_prompt(cards: list[PitfallCard]) -> str:
    """Render pitfall cards as a Markdown bullet list for the system prompt.

    Returns an empty string when there's nothing to inject so callers can
    safely concatenate without worrying about empty headers.
    """
    if not cards:
        return ""
    lines = ["## Learned Pitfalls (from prior failed/recovered runs)"]
    for card in cards:
        lines.append(f"- {card.rule}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summarization (LLM-driven)
# ---------------------------------------------------------------------------

SUMMARIZER_SYSTEM_PROMPT = """\
You are an autonomous error-pattern summarizer for a CAD-code agent.

Given a list of failure events from one or more recent agent turns, extract
GENERAL, REUSABLE lessons about CadQuery code generation. Each lesson should
read like a hard-won engineering pitfall, not a description of one specific
bug. Examples of good lessons:

- "When using .pushPoints().hole(d), make sure the chain already has a base
  solid — chain it off body.faces('>Z').workplane()."
- "Hoist all dimension parameters above any def() that closes over them; a
  nested function evaluates at call time."

Examples of BAD lessons (too specific — do not produce these):

- "On 2026-03-14, the model wrote back_wall = back_wall.translate((0, -5"
- "Line 47 had a syntax error in the iPhone holder run"

## Output (STRICT)

Respond with ONLY a JSON object — no prose, no markdown fences:

{
  "pitfalls": [
    {
      "rule": "<one-sentence generic rule the agent should follow next time>",
      "trigger_hint": "<short keyword/phrase that recognizes this class of bug, e.g. 'pushPoints solid'>"
    }
  ]
}

If no general lesson can be extracted, output ``{"pitfalls": []}``.
Cap at 3 pitfalls per call. Be specific enough to be useful, generic enough
to apply to future runs.
"""


def _render_events_for_summary(events: Iterable[FailureEvent], limit: int = 40) -> str:
    rows: list[str] = []
    for idx, e in enumerate(events):
        if idx >= limit:
            break
        rows.append(
            f"- failure_type={e.failure_type} fix={e.fix_kind} "
            f"succeeded={e.succeeded} iter={e.iteration} "
            f"error=\"{e.error_first_line[:160]}\""
        )
    return "\n".join(rows) if rows else "(no events)"


async def summarize_turn(
    llm,
    turn_events: list[FailureEvent],
    *,
    include_recent_log: int = 20,
    turn_succeeded: bool = True,
) -> list[PitfallCard]:
    """Run the LLM summarizer over a turn's events and merge new pitfalls.

    ``llm`` must expose ``generate(user_message, system_prompt, max_tokens=...)``.
    A ``LLMService`` instance from ``backend.models.llm_service`` works directly.

    ``turn_succeeded`` tells the summarizer whether the turn eventually
    produced a model. Failed turns still carry signal — when the agent
    burns its entire budget on the same kind of error, *that* is the
    lesson — but the rule wording should be framed differently so we
    don't store "always do X" for an X that didn't actually work.

    Returns the new/updated cards (whether merged into existing ones or added).
    Errors are swallowed; this is a best-effort enrichment, not critical path.
    """
    if not turn_events:
        return []
    # Only events that progressed past the "pending" placeholder count
    # as repairs the summarizer should reason about. A turn that recorded
    # failures but never attached a fix_kind isn't a journey — it's a
    # crash log.
    repair_count = sum(
        1 for e in turn_events if e.fix_kind not in ("", "none", "pending")
    )
    if repair_count < MIN_REPAIRS_FOR_SUMMARY:
        return []

    recent_log: list[FailureEvent] = []
    if include_recent_log > 0:
        recent_log = read_log(limit=include_recent_log)

    outcome_framing = (
        "The turn eventually SUCCEEDED. Each lesson should describe how to "
        "avoid the failure mode in the first place — phrasing like 'When X, "
        "do Y' or 'Avoid Z because ...'."
        if turn_succeeded
        else "The turn FAILED — the agent exhausted its repair budget and "
        "never produced a usable model. The lesson is what NOT to do, or what "
        "structural change avoids the dead-end. Phrase rules as a forbidden "
        "pattern or an early-exit signal, not as an instruction that 'works'."
    )

    user_msg = (
        f"{outcome_framing}\n\n"
        "Recent failure events from the current turn (in order):\n"
        f"{_render_events_for_summary(turn_events)}\n\n"
        "Additional context — recent failures from prior turns (most recent first):\n"
        f"{_render_events_for_summary(recent_log)}\n\n"
        "Extract 0-3 generic, reusable pitfalls. Output JSON only."
    )

    try:
        raw = await llm.generate(user_msg, SUMMARIZER_SYSTEM_PROMPT, max_tokens=1024)
    except Exception as exc:
        logger.warning("error_patterns: summarizer LLM call failed: %s", exc)
        return []

    parsed = _parse_summarizer_response(raw)
    if not parsed:
        return []

    cards = load_pitfalls()
    before_ids = {c.card_id for c in cards}
    for item in parsed:
        rule = (item.get("rule") or "").strip()
        hint = (item.get("trigger_hint") or "").strip()
        if not rule:
            continue
        _merge_card(cards, rule, hint, source_events=len(turn_events))
    cards = _evict_to_cap(cards, cap=CARDS_MAX)
    save_pitfalls(cards)

    new_or_updated = [c for c in cards if c.card_id not in before_ids or _was_just_touched(c)]
    return new_or_updated


def _was_just_touched(card: PitfallCard) -> bool:
    """Heuristic: card was updated in the last few seconds."""
    try:
        ts = datetime.fromisoformat(card.last_used)
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() < 10.0


def _parse_summarizer_response(raw: Any) -> list[dict]:
    """Parse the JSON-shaped summarizer output, tolerant to fences/preambles."""
    if not isinstance(raw, str):
        # Defensive guard for test doubles / unexpected LLM-client wrappers
        # that return non-string objects.
        return []
    text = raw
    if not text.strip():
        return []
    # Strip fences if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    # Find the first {...} block.
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        text = brace.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = data.get("pitfalls") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def schedule_summarization(
    llm,
    turn_events: list[FailureEvent],
    *,
    include_recent_log: int = 20,
    turn_succeeded: bool = True,
) -> Optional[asyncio.Task]:
    """Fire-and-forget summarization so the orchestrator never waits for it.

    Returns the created task (mostly useful for tests). If no event loop is
    running, runs the coroutine synchronously and discards the result.
    """
    if not turn_events:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    coro = summarize_turn(
        llm,
        turn_events,
        include_recent_log=include_recent_log,
        turn_succeeded=turn_succeeded,
    )
    if loop is None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            logger.warning("error_patterns: synchronous summarization failed: %s", exc)
        return None
    return loop.create_task(coro)
