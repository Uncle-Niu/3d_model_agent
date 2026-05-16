"""
Local-LLM knowledge recall.

Asks several local Ollama models, in priority order, for structured facts
about a subject. As soon as two models agree on enough fields, returns a
consensus. Anything no two models agreed on is reported as `uncertain` so the
caller can decide whether to fall back to a web search.

Why not just trust the main model alone:
    A single LLM will confidently invent dimensions for products it has not
    seen — especially anything released after its cutoff. Asking models from
    different providers gives cheap cross-provider triangulation: when Qwen
    (Alibaba) and Gemma (Google) independently produce the same body
    dimensions for "iPhone 16 Pro Max", the number is overwhelmingly likely
    correct. When they disagree, the disagreement itself is the signal.

The chain is hardcoded to 5 ~30B models from 5 different providers (Alibaba,
Google, NVIDIA, Z.ai, Liquid AI). All run locally via Ollama; nothing in this
module makes a cloud call.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Optional

from openai import AsyncOpenAI

from ..domain.models import (
    FieldValue,
    KnowledgeConsensus,
    ModelRecallResponse,
    RecallSubject,
)


# Priority order. The first model is the same one the main agent uses, so it
# is already warm in VRAM. Subsequent models cross-check from disjoint
# training corpora. Chain head pulls from `backend.config.DEFAULT_LLM_MODEL`
# so flipping the agent's default also flips the recall chain head.
from ..config import DEFAULT_LLM_MODEL as _MAIN_AGENT_MODEL

DEFAULT_MODEL_CHAIN: tuple[str, ...] = (
    _MAIN_AGENT_MODEL,        # Alibaba — main agent model, already warm in VRAM
    "gemma4:31b",             # Google — different training data
    "nemotron3:33b",          # NVIDIA — freshest cutoff (multimodal)
    "glm-4.7-flash:q4_K_M",   # Z.ai — strong in the 30B class
    "phi4:14b",               # Microsoft — small (9 GB), fast, synthetic/textbook bias; last because it's the weakest
)
# `deepseek-r1:32b` is also installed but is a reasoning model — it burns
# 30-80s thinking before answering for what is fundamentally a recall task.
# Kept on disk for manual experiments but excluded from the default chain.


# Per-step callback signature: (event_name, payload) -> awaitable.
StepCallback = Callable[[str, dict[str, Any]], Awaitable[Any]]


class LocalKnowledgeService:
    """Multi-model knowledge extractor over local Ollama models."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "ollama",
        model_chain: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
        min_agreement_ratio: float = 0.6,
        numeric_tolerance: float = 0.05,
        per_call_timeout_s: float = 90.0,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model_chain = model_chain
        # Fraction of required fields that must reach consensus before we stop
        # asking more models. 0.6 = "we have a useful answer", not "perfect".
        self.min_agreement_ratio = min_agreement_ratio
        # Two numeric values agree if their relative difference is within this
        # fraction. 0.05 = 5% — generous enough to absorb model rounding
        # (e.g. one model says 163.0, another says 163.5), tight enough to
        # catch genuine disagreement (160 vs 175).
        self.numeric_tolerance = numeric_tolerance
        self.per_call_timeout_s = per_call_timeout_s
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    # ------------------------------------------------------------------
    # Subject / field discovery
    # ------------------------------------------------------------------

    async def detect_subjects(
        self,
        user_message: str,
        main_model: str,
        on_raw: Optional[Callable[[str], Awaitable[Any]]] = None,
    ) -> list[RecallSubject]:
        """Use the main model to extract subjects + fields that, if known up
        front, would improve the CAD design. Runs ONCE per turn before any
        per-subject recall.

        Returns at most a handful of subjects. Empty list means the prompt
        does not reference any real-world object whose specs would matter
        (e.g. "make a 50mm cube"). In that case we skip recall entirely.
        """
        prompt = f"""\
You are preparing context for a CAD design agent. Identify any real-world
products, parts, or standards in the user's request whose precise dimensions
or specifications would meaningfully improve the design.

User request: {user_message}

Examples:
- "make an iphone 16 pro max holder" → subject "iPhone 16 Pro Max", fields like body_length_mm, body_width_mm, body_thickness_mm, weight_g, camera_bump_dimensions_mm, button_positions
- "design a bracket for a NEMA 17 stepper" → subject "NEMA 17 stepper motor", fields like body_size_mm, shaft_diameter_mm, mounting_hole_pattern_mm
- "make a 50x30x10mm box with rounded corners" → no subjects (purely parametric, no external reference)

Output ONLY a JSON array. Empty array if there are no real-world references.
Each entry must have:
  - "subject": short canonical name of the object
  - "fields": list of field names you would want known (use snake_case)
  - "reasoning": one sentence explaining why these fields matter for the design

Output the JSON array with no surrounding prose.
"""
        try:
            resp = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=main_model,
                    messages=[
                        {"role": "system", "content": "You output strict JSON. No prose."},
                        {"role": "user", "content": prompt + "\n\n/no_think"},
                    ],
                    temperature=0.0,
                    max_tokens=2048,
                ),
                timeout=self.per_call_timeout_s,
            )
            text = resp.choices[0].message.content or ""
            # Some Ollama models put their answer in `reasoning_content` instead
            # of `content` when /no_think isn't honoured. Combine both so we
            # never miss the JSON.
            msg = resp.choices[0].message
            reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or ""
            combined = (text or "") + ("\n" + reasoning if reasoning else "")
            if on_raw:
                await on_raw(combined)
            # Strip code fences if the model wrapped JSON in them.
            cleaned = re.sub(r"```(?:json)?", "", combined).strip()
            blob = _first_json_array(cleaned)
            if not blob:
                # The model wrapped the array in an object — try to find that.
                obj_blob = _first_json_object(cleaned)
                if obj_blob:
                    try:
                        wrap = json.loads(obj_blob)
                        # Accept several common shapes: {subjects: [...]},
                        # {items: [...]}, {results: [...]}.
                        for key in ("subjects", "items", "results", "data"):
                            v = wrap.get(key) if isinstance(wrap, dict) else None
                            if isinstance(v, list):
                                blob = json.dumps(v)
                                break
                    except Exception:
                        pass
            data = json.loads(blob or "[]")
            subjects: list[RecallSubject] = []
            for item in data[:5]:  # cap at 5 subjects per turn
                if not isinstance(item, dict):
                    continue
                subj = str(item.get("subject", "")).strip()
                fields = [str(f).strip() for f in (item.get("fields") or []) if str(f).strip()]
                if subj and fields:
                    subjects.append(RecallSubject(
                        subject=subj,
                        fields=fields[:12],  # cap fields per subject
                        reasoning=item.get("reasoning"),
                    ))
            return subjects
        except Exception:
            # Subject detection is best-effort. If it fails, the orchestrator
            # falls back to the existing web-search flow.
            return []

    # ------------------------------------------------------------------
    # Per-model recall
    # ------------------------------------------------------------------

    RECALL_SYSTEM_PROMPT = "You output strict JSON. No prose."

    @staticmethod
    def build_recall_prompt(subject: str, fields: list[str]) -> str:
        """The exact user-message prompt sent to each recall model. Exposed so
        debug UIs / scripts can show what the LLM actually saw."""
        return f"""\
You are a precise reference assistant. Output what you know about the subject
from your training data, in structured JSON. Do not invent values.

Subject: {subject}
Fields to extract: {", ".join(fields)}

Output ONLY a single JSON object — no prose, no code fences. Schema:
{{
  "subject": "{subject}",
  "fields": {{
    "<field_name>": {{
      "value": <number, string, list, or null>,
      "confidence": <0.0-1.0>,
      "note": "<optional one-line context>"
    }},
    ...
  }}
}}

Rules:
- Use null for any field you are not at least 70% confident about. Better to
  say null than to guess.
- Numeric dimensions in millimeters unless the field name says otherwise.
- Confidence guide:
    0.95+ = certainty (well-documented in training data)
    0.8   = recall the value but not exact figure
    0.6   = approximate, may be off by 10%
    0.4   = vaguely remember, treat as guess
    null  = no information

/no_think
"""

    async def _query_one_model(
        self,
        model: str,
        subject: str,
        fields: list[str],
    ) -> ModelRecallResponse:
        """Ask a single model for a structured fact dump on the subject.
        Returns the parsed response or an error-stamped record."""
        prompt = self.build_recall_prompt(subject, fields)
        t0 = time.time()
        try:
            resp = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": self.RECALL_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    max_tokens=2048,
                ),
                timeout=self.per_call_timeout_s,
            )
            elapsed = time.time() - t0
            # Some thinking-style models (Qwen3.x, Nemotron3) put their entire
            # answer into the `reasoning` field instead of `content`, even when
            # we ask for non-thinking output. We accept either and concatenate
            # so the parser never misses a JSON blob that landed in the wrong
            # channel.
            msg = resp.choices[0].message
            content = msg.content or ""
            reasoning = (
                getattr(msg, "reasoning_content", None)
                or getattr(msg, "reasoning", None)
                or ""
            )
            raw = (content + ("\n" + reasoning if reasoning else "")).strip()
            fields_parsed = _parse_fields(raw)
            return ModelRecallResponse(
                model=model,
                subject=subject,
                fields=fields_parsed,
                latency_s=elapsed,
                raw_response=raw[:4000],
            )
        except Exception as e:
            return ModelRecallResponse(
                model=model,
                subject=subject,
                fields={},
                latency_s=time.time() - t0,
                raw_response="",
                error=str(e)[:300],
            )

    # ------------------------------------------------------------------
    # Top-level: extract with multi-model agreement
    # ------------------------------------------------------------------

    async def extract_knowledge(
        self,
        subject: str,
        fields: list[str],
        on_step: Optional[StepCallback] = None,
    ) -> KnowledgeConsensus:
        """Query the model chain in order. After at least two responses,
        check consensus on each field. Stop early under two conditions:

          1. Coverage target hit — `min_agreement_ratio` of the requested
             fields already have consensus values. The remaining fields are
             genuinely unknown across models, so more calls won't help.

          2. Plateau detected — the most recent model added zero new
             consensus fields. This catches the common case where the easy
             fields (body dimensions) get cross-confirmed quickly and the
             hard fields (camera bump tolerances, button positions) are
             *uniformly* missing from every model's training. Asking more
             ~30B models won't surface knowledge that none of them have.

        Each model's response is emitted to `on_step` for live UI updates.
        """
        responses: list[ModelRecallResponse] = []
        prev_consensus_count = 0
        prompt_text = self.build_recall_prompt(subject, fields)
        for idx, model in enumerate(self.model_chain):
            if on_step:
                await on_step("model_start", {
                    "model": model,
                    "subject": subject,
                    "system_prompt": self.RECALL_SYSTEM_PROMPT,
                    "prompt": prompt_text,
                })
            r = await self._query_one_model(model, subject, fields)
            responses.append(r)
            if on_step:
                await on_step("model_done", {
                    "model": model,
                    "subject": subject,
                    "latency_s": r.latency_s,
                    "field_count": sum(1 for v in r.fields.values() if v.value is not None),
                    "error": r.error,
                    "fields": {
                        fname: {"value": fv.value, "confidence": fv.confidence}
                        for fname, fv in r.fields.items()
                    },
                })
            if len(responses) < 2:
                continue
            consensus = self._compute_consensus(subject, fields, responses)
            current_count = len(consensus.fields)
            # Hard stop: we hit the coverage target.
            if consensus.is_complete(fields, self.min_agreement_ratio):
                return consensus
            # Soft stop: plateau. Require at least 3 models polled, current
            # consensus non-empty, and the latest model added no new fields.
            if (
                idx >= 2
                and current_count > 0
                and current_count == prev_consensus_count
            ):
                return consensus
            prev_consensus_count = current_count
        # Chain exhausted — return whatever consensus we managed to build.
        return self._compute_consensus(subject, fields, responses)

    def _compute_consensus(
        self,
        subject: str,
        required_fields: list[str],
        responses: list[ModelRecallResponse],
    ) -> KnowledgeConsensus:
        """Merge responses field-by-field. A field reaches consensus when at
        least two models gave non-null values that agree within tolerance.
        The chosen value is the average (for numbers) or the most common
        (for strings/lists)."""
        agreed: dict[str, FieldValue] = {}
        uncertain: list[str] = []
        contributors: set[str] = set()

        # Use the union of all fields the models actually answered, plus the
        # required fields, so we don't silently drop bonus knowledge.
        all_field_names = set(required_fields)
        for r in responses:
            all_field_names.update(r.fields.keys())

        for fname in all_field_names:
            # Collect all non-null answers for this field.
            answers = [
                (r.model, r.fields[fname])
                for r in responses
                if fname in r.fields and r.fields[fname].value is not None
            ]
            if len(answers) < 2:
                # Single or zero answers → no cross-check, mark uncertain.
                if fname in required_fields:
                    uncertain.append(fname)
                continue

            # Group answers by value-equivalence and find the largest group.
            groups = _group_agreeing(answers, self.numeric_tolerance)
            best = max(groups, key=len)
            if len(best) >= 2:
                merged_value = _merge_values([fv.value for _, fv in best])
                merged_conf = sum(fv.confidence for _, fv in best) / len(best)
                # If multiple groups have ≥2 votes, prefer the larger one
                # and record disagreement in the note.
                note = None
                if len(best) < len(answers):
                    others = [_pretty(fv.value) for _, fv in answers if not _in_group(fv, best, self.numeric_tolerance)]
                    if others:
                        note = f"{len(best)}/{len(answers)} models agreed; outliers said: {', '.join(others[:3])}"
                agreed[fname] = FieldValue(
                    value=merged_value,
                    confidence=merged_conf,
                    note=note,
                )
                for m, _ in best:
                    contributors.add(m)
            else:
                if fname in required_fields:
                    uncertain.append(fname)

        return KnowledgeConsensus(
            subject=subject,
            fields=agreed,
            contributing_models=sorted(contributors),
            uncertain_fields=sorted(set(uncertain)),
            all_responses=responses,
        )


# ----------------------------------------------------------------------
# JSON parsing / value comparison helpers
# ----------------------------------------------------------------------

def _first_json_array(text: str) -> Optional[str]:
    """Find the first top-level JSON array in `text`. Returns the substring
    starting with `[` and ending with the matching `]`, or None."""
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            if start == -1:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start:i + 1]
    return None


def _first_json_object(text: str) -> Optional[str]:
    """Find the first top-level JSON object in `text`. Same approach as
    _first_json_array but balanced on `{` / `}`."""
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if start == -1:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start:i + 1]
    return None


def _parse_fields(raw: str) -> dict[str, FieldValue]:
    """Parse a model's JSON response into FieldValue objects. Returns an empty
    dict on any failure — the caller (consensus builder) treats absent fields
    as 'this model didn't know'."""
    if not raw:
        return {}
    # Strip code fences if present.
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    blob = _first_json_object(cleaned) or cleaned
    try:
        data = json.loads(blob)
    except Exception:
        return {}
    fields_in = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields_in, dict):
        return {}
    out: dict[str, FieldValue] = {}
    for fname, fdata in fields_in.items():
        if not isinstance(fname, str):
            continue
        if not isinstance(fdata, dict):
            # Tolerate "field": value shorthand.
            out[fname] = FieldValue(value=fdata, confidence=0.5)
            continue
        try:
            out[fname] = FieldValue(
                value=fdata.get("value"),
                confidence=float(fdata.get("confidence", 0.0) or 0.0),
                note=(str(fdata["note"]) if fdata.get("note") else None),
            )
        except Exception:
            continue
    return out


def _values_agree(a: Any, b: Any, tol: float) -> bool:
    """Loose equivalence:
       - Numbers agree within relative tolerance `tol`.
       - Lists of numbers agree elementwise (same length, all within tol).
       - Strings agree after lowercase/strip.
       - Dicts agree if all shared keys agree.
       - null never agrees with anything.
    """
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        denom = max(abs(a), abs(b), 1e-6)
        return abs(a - b) / denom <= tol
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_values_agree(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        shared = set(a.keys()) & set(b.keys())
        if not shared:
            return False
        return all(_values_agree(a[k], b[k], tol) for k in shared)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _group_agreeing(
    answers: list[tuple[str, FieldValue]],
    tol: float,
) -> list[list[tuple[str, FieldValue]]]:
    """Cluster (model, value) pairs by mutual agreement under _values_agree."""
    groups: list[list[tuple[str, FieldValue]]] = []
    for entry in answers:
        _, fv = entry
        placed = False
        for g in groups:
            if _values_agree(fv.value, g[0][1].value, tol):
                g.append(entry)
                placed = True
                break
        if not placed:
            groups.append([entry])
    return groups


def _in_group(fv: FieldValue, group: list[tuple[str, FieldValue]], tol: float) -> bool:
    return any(_values_agree(fv.value, g[1].value, tol) for g in group)


def _merge_values(values: list[Any]) -> Any:
    """Pick a representative value from a group that already agreed:
       - For numbers, return the mean (rounded to 2 decimals).
       - For lists of numbers, mean elementwise.
       - For everything else, return the first value (they're equivalent
         under _values_agree's looser comparison, so the first is fine).
    """
    if not values:
        return None
    if all(isinstance(v, (int, float)) for v in values):
        return round(sum(values) / len(values), 2)
    if all(isinstance(v, list) and all(isinstance(x, (int, float)) for x in v) for v in values):
        if len(set(len(v) for v in values)) != 1:
            return values[0]
        n = len(values[0])
        return [round(sum(v[i] for v in values) / len(values), 2) for i in range(n)]
    return values[0]


def _pretty(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return "[" + ", ".join(_pretty(x) for x in v) + "]"
    return str(v)


# ----------------------------------------------------------------------
# Prompt-context formatter
# ----------------------------------------------------------------------

def format_recall_for_prompt(consensuses: list[KnowledgeConsensus]) -> str:
    """Format consensus knowledge for inclusion in the planner / generator
    prompt. Only emits fields that reached consensus — uncertain ones are
    deliberately left out so the planner asks for them or uses defaults."""
    if not consensuses:
        return ""
    lines = ["## Local-LLM Knowledge Recall (cross-checked across models)"]
    for c in consensuses:
        if not c.fields:
            continue
        contrib = ", ".join(c.contributing_models) or "(unknown)"
        lines.append(f"### {c.subject}  — agreed by {contrib}")
        for fname, fv in c.fields.items():
            note = f"  // {fv.note}" if fv.note else ""
            lines.append(f"- {fname}: {_pretty(fv.value)} (conf={fv.confidence:.2f}){note}")
        if c.uncertain_fields:
            lines.append(f"- (uncertain: {', '.join(c.uncertain_fields)})")
    return "\n".join(lines)
