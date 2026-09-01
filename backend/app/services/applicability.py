"""The applicability engine — which controls apply to this company (Gap 1).

The policy defines what must be proven; applicability decides *whether it must be
proven here at all*. That decision is mechanical: a control declares conditions
over the company profile, and this module evaluates them.

**It reuses `rule_engine.evaluate` rather than reimplementing comparison.** A
profile answer satisfies the engine's existing `Fact` protocol, so a ten-line
adapter buys the whole tested operator surface — normalisation, type coercion,
the presence branches, contradiction handling. A second comparison
implementation would be the kind of thing that silently drifts from the one with
612 tests behind it.

## The safety property this module exists to protect

`UNDETERMINED` must never collapse into `NOT_APPLICABLE`. Excluding a PCI control
because a company never answered a profile question is the same
"absence implies compliance" error the deterministic core was built to remove —
and unlike a wrong PASS, nobody would ever see it, because the control simply
would not appear.

Three things defend it:

1. A profile key that is **absent** emits no fact at all, so a condition on it
   resolves to INSUFFICIENT_EVIDENCE → `UNDETERMINED`.
2. A key that is **present but empty** (`"data_types": []`) emits `false` for
   every member — an answered "none of these" is a real answer and is allowed to
   exclude a control.
3. `EXISTS` / `NOT_EXISTS` are rejected at authoring time. They return PASS/FAIL
   for a missing fact and so could never produce `UNDETERMINED` — a condition
   using one would convert silence into exclusion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.models.enums import (
    ApplicabilityStatus,
    CloudProvider,
    DataType,
    EvaluationMode,
    EvaluationResult,
    FactValueType,
    RuleOperator,
    SystemComponent,
)
from app.services import rule_engine

logger = logging.getLogger(__name__)

# Operators that cannot express "I don't know" and are therefore forbidden in an
# applicability condition (see the module docstring).
FORBIDDEN_OPERATORS = frozenset({RuleOperator.EXISTS, RuleOperator.NOT_EXISTS})

# Set-valued profile keys, and the vocabulary each is drawn from. A key here is
# flattened to one boolean fact per member, e.g. `data_types.pan`.
#
# The dotted expansion is not cosmetic: joining a list into a string and matching
# with CONTAINS would collide on shared prefixes (`aws` inside `aws_govcloud`)
# and could not distinguish "answered, not selected" from "never asked".
SET_VALUED: dict[str, type[StrEnum]] = {
    "systems": SystemComponent,
    "data_types": DataType,
    "cloud_providers": CloudProvider,
}


@dataclass
class ProfileFact:
    """One answered profile attribute, shaped to satisfy `rule_engine.Fact`.

    `observed_at` is always None — a company profile is a present-tense statement
    and has no freshness window, so the engine's staleness path never fires.
    """

    name: str
    value: str | None
    value_type: FactValueType
    observed_at: datetime | None = None
    id: str = "profile"


@dataclass
class ApplicabilityOutcome:
    status: ApplicabilityStatus
    # Serialised rule outcomes: what was checked, what the profile said, and how
    # each condition resolved. Persisted so an auditor asking "why is 12.9.1 out
    # of scope?" gets the exact condition rather than a bare verdict.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)

    @property
    def in_scope(self) -> bool:
        return self.status is ApplicabilityStatus.IN_SCOPE


def profile_facts(profile: dict[str, Any]) -> list[ProfileFact]:
    """Flatten a company profile into facts the rule engine can evaluate.

    Scalars become one fact each. Set-valued keys become one boolean fact per
    vocabulary member — present in the list is `true`, absent from an *answered*
    list is `false`, and an unanswered key emits nothing at all.
    """
    facts: list[ProfileFact] = []

    for key, value in profile.items():
        if key in SET_VALUED:
            continue
        if value is None:
            # Explicit null is "not answered", same as omitting the key.
            continue
        if isinstance(value, bool):
            facts.append(
                ProfileFact(
                    name=key, value="true" if value else "false", value_type=FactValueType.boolean
                )
            )
        elif isinstance(value, int):
            facts.append(ProfileFact(name=key, value=str(value), value_type=FactValueType.integer))
        else:
            facts.append(ProfileFact(name=key, value=str(value), value_type=FactValueType.string))

    for key, vocabulary in SET_VALUED.items():
        if key not in profile or profile[key] is None:
            # Never asked. Emit nothing, so any condition on this family lands on
            # UNDETERMINED rather than being answered "no" by default.
            continue
        selected = {str(v) for v in profile[key]}
        for member in vocabulary:
            facts.append(
                ProfileFact(
                    name=f"{key}.{member.value}",
                    value="true" if member.value in selected else "false",
                    value_type=FactValueType.boolean,
                )
            )

    facts.extend(_derived(profile))
    return facts


def _derived(profile: dict[str, Any]) -> list[ProfileFact]:
    """Facts computed from other answers.

    This is how OR is expressed. The condition grammar is deliberately AND-only —
    a nested boolean grammar would be a second little language to author, test and
    get wrong — so a genuine disjunction like PCI's "applies if you store *or*
    transmit cardholder data" is precomputed here instead.

    ponytail: AND-only conditions; OR lives here as a derived fact. Add a nested
    grammar only if a condition ever needs OR over facts this cannot precompute.
    """
    stores = profile.get("stores_cardholder_data")
    transmits = profile.get("transmits_cardholder_data")
    if stores is None and transmits is None:
        return []
    return [
        ProfileFact(
            name="handles_cardholder_data",
            value="true" if (stores or transmits) else "false",
            value_type=FactValueType.boolean,
        )
    ]


def evaluate(conditions: list[dict[str, Any]], profile: dict[str, Any]) -> ApplicabilityOutcome:
    """Decide whether a control applies to this company.

    Mapping from the rule engine's result vocabulary:

    | engine result                      | applicability   |
    |------------------------------------|-----------------|
    | PASS (all conditions hold)         | IN_SCOPE        |
    | FAIL (a condition definitively no) | NOT_APPLICABLE  |
    | INSUFFICIENT_EVIDENCE / PARTIAL    | UNDETERMINED    |
    | CONFLICT                           | UNDETERMINED    |
    """
    if not conditions:
        # A control that states no condition applies universally. Short-circuited
        # because the engine treats an empty ruleset as INSUFFICIENT_EVIDENCE,
        # which is the right answer for evidence and the wrong one here.
        return ApplicabilityOutcome(status=ApplicabilityStatus.IN_SCOPE)

    facts = profile_facts(profile)
    outcome = rule_engine.evaluate(
        rules=conditions,
        facts=facts,
        # Constants: applicability is always a mechanical comparison, and a
        # company profile has no freshness window.
        evaluation_mode=EvaluationMode.DETERMINISTIC,
        freshness_window_days=None,
    )

    if outcome.result is EvaluationResult.PASS:
        status = ApplicabilityStatus.IN_SCOPE
    elif outcome.result is EvaluationResult.FAIL:
        status = ApplicabilityStatus.NOT_APPLICABLE
    else:
        # PARTIAL, INSUFFICIENT_EVIDENCE and CONFLICT all mean the profile did not
        # settle the question. None of them may exclude a control.
        status = ApplicabilityStatus.UNDETERMINED

    answered = {f.name for f in facts}
    unanswered = sorted(
        {str(c.get("fact")) for c in conditions if str(c.get("fact")) not in answered}
    )

    return ApplicabilityOutcome(
        status=status,
        evidence=[
            {
                "fact": o.fact_name,
                "operator": o.operator,
                "expected": o.expected,
                "actual": o.actual,
                "result": o.result.value,
                "detail": o.detail,
            }
            for o in outcome.rule_outcomes
        ],
        unanswered=unanswered,
    )


def validate_conditions(conditions: list[dict[str, Any]]) -> list[str]:
    """Return authoring errors for a control's applicability conditions.

    Used by the corpus loader and the authoring API. Returning a list rather than
    raising lets a caller report every problem in one pass instead of one per
    save.
    """
    errors: list[str] = []
    known = _known_attribute_names()

    for condition in conditions:
        fact = str(condition.get("fact", ""))
        operator = str(condition.get("operator", ""))

        try:
            parsed = RuleOperator(operator)
        except ValueError:
            errors.append(f"unknown operator {operator!r}")
            continue

        if parsed in FORBIDDEN_OPERATORS:
            errors.append(
                f"operator {operator} is not allowed in an applicability condition: "
                "it cannot express UNDETERMINED, so an unanswered profile question "
                "would silently exclude the control"
            )

        if fact not in known:
            # A typo here would be permanently invisible — the condition would
            # resolve UNDETERMINED forever and look like an unanswered question.
            errors.append(f"condition references unknown profile attribute {fact!r}")

    return errors


def _known_attribute_names() -> set[str]:
    """Every attribute name a condition may reference.

    Derived from the schema rather than hand-listed, so adding a profile field
    cannot leave this stale.
    """
    from app.schemas.audit import CompanyProfile

    names: set[str] = set()
    for key in CompanyProfile.model_fields:
        if key in SET_VALUED:
            names.update(f"{key}.{member.value}" for member in SET_VALUED[key])
        else:
            names.add(key)
    # Folded in from the Audit columns, and computed in `_derived`.
    names.update({"entity_type", "merchant_level", "handles_cardholder_data"})
    return names
