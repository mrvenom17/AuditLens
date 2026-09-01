"""The deterministic rule engine (01_REQUIREMENTS.md § Deterministic Rule
Evaluation, TASK-107).

This module is the product's trust claim in executable form. It answers one
question — "what do the rules, applied mechanically to provenanced facts,
produce" — and it answers it with no network, no model, and no inference.

Three properties are load-bearing, and each is tested rather than merely
intended:

* **Zero LLM dependency.** This module imports nothing from `app.pipelines.llm`
  or `app.pipelines.embedding`, directly or transitively. `tests/adversarial/`
  asserts that by import inspection, so the property survives contributors who
  never read the docs (06_ENGINEERING_RULES.md § Architecture Discipline).
* **Purity.** Same facts plus same rules always produce the same result. No
  clock reads except the one explicitly passed in as `now`, no database, no I/O.
* **Honest failure.** When the engine cannot determine a result it says
  INSUFFICIENT_EVIDENCE or CONFLICT and stops. It never guesses, never averages
  contradictory evidence, and never delegates to a model
  (06_ENGINEERING_RULES.md § The Deterministic Core Invariant).

Nothing here parses evidence *content*. It reads already-extracted `EvidenceFact`
values, which is why a prompt-injection payload sitting in a source document has
no mechanism by which to reach this code — there is no branch anywhere below
that document text can influence.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.models.enums import EvaluationMode, EvaluationResult, FactValueType, RuleOperator

logger = logging.getLogger(__name__)

ENGINE_VERSION = "1.0.0"


class Fact(Protocol):
    """The shape the engine needs. `EvidenceFact` satisfies it, and so does a
    plain stub in a unit test — which is the point: the engine is testable with
    no database and no application context."""

    name: str
    value: str | None
    value_type: FactValueType
    observed_at: datetime | None


class UnsupportedOperatorError(ValueError):
    """Raised only for an operator outside the fixed set. Authoring-time
    validation should make this unreachable at evaluation time; if it is ever
    raised, a malformed control reached the engine and that is a bug worth
    surfacing loudly rather than degrading around."""


@dataclass(frozen=True)
class RuleOutcome:
    """One rule's contribution, kept individually so the review UI can show the
    auditor exactly which comparison drove the verdict rather than a bare
    PASS/FAIL for the whole control."""

    fact_name: str
    operator: str
    expected: Any
    actual: Any | None
    result: EvaluationResult
    detail: str


@dataclass
class EvaluationOutcome:
    result: EvaluationResult
    rule_outcomes: list[RuleOutcome] = field(default_factory=list)
    facts_used: list[Any] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    stale: bool = False
    engine_version: str = ENGINE_VERSION


# Which state wins when a control's rules disagree. Ordered most- to
# least-dominant, and deliberately explicit rather than inferred at runtime
# (01_REQUIREMENTS.md § Processing Rules, item 5):
#
#   CONFLICT   — the evidence base contradicts itself, so no verdict on this
#                control is trustworthy until a human resolves it. It outranks
#                even a definite FAIL, because a contradiction means we cannot
#                rely on *any* fact for this control.
#   FAIL       — at least one rule definitively failed. Unknowns elsewhere
#                cannot rescue it: the control cannot pass.
#   PARTIAL    — some rules passed, others lack evidence. Genuinely partial.
#   INSUFFICIENT_EVIDENCE — nothing could be determined at all.
#   PASS       — every rule passed.
_PRECEDENCE = (
    EvaluationResult.CONFLICT,
    EvaluationResult.FAIL,
    EvaluationResult.INSUFFICIENT_EVIDENCE,
    EvaluationResult.PASS,
)


def evaluate(
    *,
    rules: list[dict[str, Any]],
    facts: Sequence[Fact],
    evaluation_mode: EvaluationMode,
    required_facts: Sequence[str] = (),
    freshness_window_days: int | None = None,
    applicable: bool = True,
    now: datetime | None = None,
) -> EvaluationOutcome:
    """Evaluate one control's rules against the facts extracted for it.

    `now` is a parameter rather than a `datetime.now()` call so freshness is
    reproducible in a test and in a re-run — an engine whose output depends on
    an implicit clock is not deterministic in the sense this product claims.
    """
    if not applicable:
        return EvaluationOutcome(result=EvaluationResult.NOT_APPLICABLE)

    # HUMAN_ASSISTED controls are never evaluated here. Returning a mechanical
    # verdict for one would be exactly the dishonesty 00_PRODUCT.md §5.7 rules
    # out — the Evidence Gate independently rejects any that slips through
    # (check VALID_EVALUATION_METHOD), so this is defence in depth, not the only
    # guard.
    if evaluation_mode == EvaluationMode.HUMAN_ASSISTED:
        return EvaluationOutcome(
            result=EvaluationResult.INSUFFICIENT_EVIDENCE,
            rule_outcomes=[
                RuleOutcome(
                    fact_name="-",
                    operator="-",
                    expected=None,
                    actual=None,
                    result=EvaluationResult.INSUFFICIENT_EVIDENCE,
                    detail=(
                        "This control requires human interpretation and is not machine-evaluable."
                    ),
                )
            ],
        )

    # STRUCTURED controls take a different question entirely. DETERMINISTIC asks
    # "is the configured value acceptable"; STRUCTURED asks "is the required
    # information present and well-formed at all" — the required-document /
    # required-field check. Routed before the operator loop because it consults
    # `required_facts`, not `rules`.
    if evaluation_mode == EvaluationMode.STRUCTURED:
        return _evaluate_structured(
            required_facts, facts, freshness_window_days=freshness_window_days, now=now
        )

    if not rules:
        # A DETERMINISTIC control cannot reach here — the authoring validation
        # and the database CHECK both reject a ruleless one.
        return EvaluationOutcome(result=EvaluationResult.INSUFFICIENT_EVIDENCE)

    now = now or datetime.now(UTC)
    by_name = _group_by_name(facts)

    outcomes: list[RuleOutcome] = []
    used: list[Any] = []
    contradictions: list[dict[str, Any]] = []
    stale = False

    for rule in rules:
        fact_name = str(rule.get("fact", ""))
        operator = str(rule.get("operator", ""))
        expected = rule.get("expected")
        candidates = by_name.get(fact_name, [])

        # Contradiction check runs before the comparison. Two sources disagreeing
        # is never resolved by preferring one — 01_REQUIREMENTS.md forbids
        # picking "the more recent" or "the more confident" without an explicit
        # human-authored tie-break, and no such tie-break exists at Level 0.
        distinct = {_normalise(f.value, f.value_type) for f in candidates}
        if len(distinct) > 1:
            contradictions.append(
                {
                    "fact": fact_name,
                    "values": sorted(str(v) for v in distinct),
                    "fact_ids": [str(getattr(f, "id", "")) for f in candidates],
                }
            )
            outcomes.append(
                RuleOutcome(
                    fact_name=fact_name,
                    operator=operator,
                    expected=expected,
                    actual=None,
                    result=EvaluationResult.CONFLICT,
                    detail=(
                        f"Evidence disagrees on '{fact_name}': "
                        f"{', '.join(sorted(str(v) for v in distinct))}."
                    ),
                )
            )
            used.extend(candidates)
            continue

        outcome, fact = _apply(fact_name, operator, expected, candidates)
        outcomes.append(outcome)
        if fact is not None:
            used.append(fact)
            if _is_stale(fact, freshness_window_days, now):
                stale = True

    return EvaluationOutcome(
        result=_combine(outcomes),
        rule_outcomes=outcomes,
        facts_used=used,
        contradictions=contradictions,
        stale=stale,
    )


def _evaluate_structured(
    required_facts: Sequence[str],
    facts: Sequence[Fact],
    *,
    freshness_window_days: int | None = None,
    now: datetime | None = None,
) -> EvaluationOutcome:
    """Check that every declared fact is present and well-formed.

    Values are never judged here — a password length of 4 is a perfectly valid
    *structured* answer, and whether 4 is acceptable is a DETERMINISTIC question.
    What this path catches is the document that omits a required field entirely,
    or states it in a form that will not parse.

    A malformed value is FAIL rather than INSUFFICIENT_EVIDENCE: the evidence was
    provided and is structurally wrong, which the document itself demonstrates.
    That is a finding, not a gap.
    """
    now = now or datetime.now(UTC)

    if not required_facts:
        # The database CHECK and the authoring validation both reject a STRUCTURED
        # control with no declared facts, so this is unreachable in practice. It
        # is still not a pass — checking nothing never demonstrates anything.
        return EvaluationOutcome(result=EvaluationResult.INSUFFICIENT_EVIDENCE)

    by_name = _group_by_name(facts)
    outcomes: list[RuleOutcome] = []
    used: list[Any] = []
    contradictions: list[dict[str, Any]] = []
    stale = False

    for name in required_facts:
        candidates = by_name.get(name, [])

        if not candidates:
            outcomes.append(
                RuleOutcome(
                    fact_name=name,
                    operator="REQUIRED",
                    expected="present",
                    actual=None,
                    result=EvaluationResult.INSUFFICIENT_EVIDENCE,
                    detail=f"'{name}' is required by this control but was not found.",
                )
            )
            continue

        distinct = {_normalise(f.value, f.value_type) for f in candidates}
        if len(distinct) > 1:
            contradictions.append(
                {
                    "fact": name,
                    "values": sorted(str(v) for v in distinct),
                    "fact_ids": [str(getattr(f, "id", "")) for f in candidates],
                }
            )
            outcomes.append(
                RuleOutcome(
                    fact_name=name,
                    operator="REQUIRED",
                    expected="present",
                    actual=None,
                    result=EvaluationResult.CONFLICT,
                    detail=(
                        f"Evidence disagrees on '{name}': "
                        f"{', '.join(sorted(str(v) for v in distinct))}."
                    ),
                )
            )
            used.extend(candidates)
            continue

        fact = candidates[0]
        value = _normalise(fact.value, fact.value_type)
        used.append(fact)
        if _is_stale(fact, freshness_window_days, now):
            stale = True

        if value is None:
            outcomes.append(
                RuleOutcome(
                    fact_name=name,
                    operator="REQUIRED",
                    expected=f"a readable {fact.value_type.value}",
                    actual=fact.value,
                    result=EvaluationResult.FAIL,
                    detail=(
                        f"'{name}' is present but could not be read as {fact.value_type.value}."
                    ),
                )
            )
        else:
            outcomes.append(
                RuleOutcome(
                    fact_name=name,
                    operator="REQUIRED",
                    expected="present",
                    actual=value,
                    result=EvaluationResult.PASS,
                    detail=f"{name} = {value!r} is present and well-formed.",
                )
            )

    return EvaluationOutcome(
        result=_combine(outcomes),
        rule_outcomes=outcomes,
        facts_used=used,
        contradictions=contradictions,
        stale=stale,
    )


def _combine(outcomes: list[RuleOutcome]) -> EvaluationResult:
    """Apply the documented combination rule.

    PARTIAL is reserved for the genuinely mixed case — something was
    demonstrated and something else could not be checked. Reporting that as a
    flat INSUFFICIENT_EVIDENCE would understate what the evidence did show;
    reporting it as PASS would overstate it.
    """
    results = {o.result for o in outcomes}
    for state in _PRECEDENCE:
        if state in results:
            if state == EvaluationResult.INSUFFICIENT_EVIDENCE and EvaluationResult.PASS in results:
                return EvaluationResult.PARTIAL
            return state
    return EvaluationResult.INSUFFICIENT_EVIDENCE


def _apply(
    fact_name: str, operator: str, expected: Any, candidates: list[Fact]
) -> tuple[RuleOutcome, Fact | None]:
    """Apply one operator. Presence operators are handled first because they are
    the only two that are meaningful when no fact exists."""
    try:
        op = RuleOperator(operator)
    except ValueError as exc:
        raise UnsupportedOperatorError(f"Unsupported rule operator: {operator!r}") from exc

    present = bool(candidates)

    if op == RuleOperator.EXISTS:
        return (
            RuleOutcome(
                fact_name=fact_name,
                operator=operator,
                expected=True,
                actual=present,
                result=EvaluationResult.PASS if present else EvaluationResult.FAIL,
                detail=f"'{fact_name}' {'was' if present else 'was not'} found in the evidence.",
            ),
            candidates[0] if present else None,
        )

    if op == RuleOperator.NOT_EXISTS:
        return (
            RuleOutcome(
                fact_name=fact_name,
                operator=operator,
                expected=False,
                actual=present,
                result=EvaluationResult.FAIL if present else EvaluationResult.PASS,
                detail=f"'{fact_name}' {'was' if present else 'was not'} found in the evidence.",
            ),
            candidates[0] if present else None,
        )

    if not present:
        # 01_REQUIREMENTS.md item 2, and the hallucination-rejection requirement:
        # a missing fact is INSUFFICIENT_EVIDENCE. Never a guess, never a
        # default, never "absence implies compliance".
        return (
            RuleOutcome(
                fact_name=fact_name,
                operator=operator,
                expected=expected,
                actual=None,
                result=EvaluationResult.INSUFFICIENT_EVIDENCE,
                detail=f"No evidence was found for '{fact_name}'.",
            ),
            None,
        )

    fact = candidates[0]
    actual = _normalise(fact.value, fact.value_type)
    if actual is None:
        return (
            RuleOutcome(
                fact_name=fact_name,
                operator=operator,
                expected=expected,
                actual=None,
                result=EvaluationResult.INSUFFICIENT_EVIDENCE,
                detail=f"'{fact_name}' was found but its value could not be read as "
                f"{fact.value_type.value}.",
            ),
            fact,
        )

    passed = _compare(op, actual, expected, fact.value_type)
    return (
        RuleOutcome(
            fact_name=fact_name,
            operator=operator,
            expected=expected,
            actual=actual,
            result=EvaluationResult.PASS if passed else EvaluationResult.FAIL,
            detail=f"{fact_name} = {actual!r}; required {operator} {expected!r}.",
        ),
        fact,
    )


def _compare(op: RuleOperator, actual: Any, expected: Any, value_type: FactValueType) -> bool:
    """The mechanical comparison. No branch here depends on anything but the two
    values and the operator."""
    if op in (RuleOperator.IN, RuleOperator.NOT_IN):
        options = expected if isinstance(expected, list | tuple | set) else [expected]
        coerced = [_coerce(o, value_type) for o in options]
        hit = actual in coerced
        return hit if op == RuleOperator.IN else not hit

    if op == RuleOperator.CONTAINS:
        if isinstance(actual, str):
            return str(expected).lower() in actual.lower()
        if isinstance(actual, list | tuple | set):
            return expected in actual
        return False

    other = _coerce(expected, value_type)
    if other is None:
        return False

    if op == RuleOperator.EQ:
        return bool(actual == other)
    if op == RuleOperator.NE:
        return bool(actual != other)

    # Ordering operators are only meaningful on ordered types. Comparing two
    # booleans with >= would silently "work" in Python (True > False) and mean
    # nothing, so it is rejected rather than answered.
    if value_type == FactValueType.boolean:
        raise UnsupportedOperatorError(f"Operator {op.value} is not meaningful on a boolean fact.")
    try:
        if op == RuleOperator.GT:
            return bool(actual > other)
        if op == RuleOperator.GTE:
            return bool(actual >= other)
        if op == RuleOperator.LT:
            return bool(actual < other)
        if op == RuleOperator.LTE:
            return bool(actual <= other)
    except TypeError:
        return False

    raise UnsupportedOperatorError(f"Unsupported rule operator: {op.value}")


def _normalise(raw: str | None, value_type: FactValueType) -> Any:
    """Cast a stored fact value to its declared type, or None if it will not
    cast. An uncastable value is INSUFFICIENT_EVIDENCE, never a coerced guess."""
    if raw is None:
        return None
    return _coerce(raw, value_type)


def _coerce(raw: Any, value_type: FactValueType) -> Any:
    if raw is None:
        return None
    if value_type == FactValueType.integer:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return None
    if value_type == FactValueType.boolean:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "yes", "enabled", "on", "1"):
            return True
        if text in ("false", "no", "disabled", "off", "0"):
            return False
        return None
    if value_type == FactValueType.date:
        text = str(raw).strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return str(raw).strip()


def _group_by_name(facts: Sequence[Fact]) -> dict[str, list[Fact]]:
    grouped: dict[str, list[Fact]] = {}
    for fact in facts:
        grouped.setdefault(fact.name, []).append(fact)
    return grouped


def _is_stale(fact: Fact, window_days: int | None, now: datetime) -> bool:
    """Freshness is measured on `observed_at` — when the evidence says the fact
    was true — not on when we happened to extract it. A year-old config export
    processed this morning is stale evidence, and dating it by extraction time
    would hide exactly that."""
    if window_days is None or fact.observed_at is None:
        return False
    observed = fact.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return observed < now - timedelta(days=window_days)
