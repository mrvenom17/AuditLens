"""Rule engine tests (TASK-107).

08_TESTING.md requires every operator individually, the rule-combination logic,
and the four core acceptance scenarios. It also requires proof that the engine
has zero LLM dependency — `TestZeroLLMDependency` covers that two ways: by
import inspection, and by running the whole engine with any LLM or embedding
call wired to raise.

Every test here constructs facts in memory. Nothing touches a database, which is
itself the point: an engine that needs application context to evaluate a rule is
not the pure function this product claims it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import EvaluationMode, EvaluationResult, FactValueType
from app.services import rule_engine
from app.services.rule_engine import UnsupportedOperatorError, evaluate

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@dataclass
class StubFact:
    """Satisfies the engine's `Fact` protocol with no ORM and no session."""

    name: str
    value: str | None
    value_type: FactValueType = FactValueType.integer
    observed_at: datetime | None = None
    id: str = "fact-1"


def one(name: str = "minimum_password_length", value: str = "14", **kwargs: object) -> StubFact:
    return StubFact(name=name, value=value, **kwargs)  # type: ignore[arg-type]


def run(rules: list[dict[str, object]], facts: list[StubFact], **kwargs: object):  # type: ignore[no-untyped-def]
    return evaluate(
        rules=rules,
        facts=facts,
        evaluation_mode=kwargs.pop("evaluation_mode", EvaluationMode.DETERMINISTIC),  # type: ignore[arg-type]
        now=kwargs.pop("now", NOW),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class TestOperators:
    """08_TESTING.md: "Every rule-engine operator individually"."""

    @pytest.mark.parametrize(
        ("operator", "actual", "expected", "result"),
        [
            ("==", "14", 14, EvaluationResult.PASS),
            ("==", "8", 14, EvaluationResult.FAIL),
            ("!=", "8", 14, EvaluationResult.PASS),
            ("!=", "14", 14, EvaluationResult.FAIL),
            (">", "15", 14, EvaluationResult.PASS),
            (">", "14", 14, EvaluationResult.FAIL),
            (">=", "14", 12, EvaluationResult.PASS),
            (">=", "8", 12, EvaluationResult.FAIL),
            ("<", "8", 10, EvaluationResult.PASS),
            ("<", "10", 10, EvaluationResult.FAIL),
            ("<=", "10", 10, EvaluationResult.PASS),
            ("<=", "11", 10, EvaluationResult.FAIL),
        ],
    )
    def test_numeric_operators(
        self, operator: str, actual: str, expected: int, result: EvaluationResult
    ) -> None:
        outcome = run(
            [{"fact": "n", "operator": operator, "expected": expected}],
            [one("n", actual)],
        )
        assert outcome.result == result

    @pytest.mark.parametrize(
        ("operator", "actual", "expected", "result"),
        [
            ("IN", "1.2", ["1.2", "1.3"], EvaluationResult.PASS),
            ("IN", "1.0", ["1.2", "1.3"], EvaluationResult.FAIL),
            ("NOT_IN", "1.0", ["1.2", "1.3"], EvaluationResult.PASS),
            ("NOT_IN", "1.3", ["1.2", "1.3"], EvaluationResult.FAIL),
        ],
    )
    def test_membership_operators(
        self, operator: str, actual: str, expected: list[str], result: EvaluationResult
    ) -> None:
        outcome = run(
            [{"fact": "tls", "operator": operator, "expected": expected}],
            [one("tls", actual, value_type=FactValueType.string)],
        )
        assert outcome.result == result

    @pytest.mark.parametrize(
        ("actual", "expected", "result"),
        [
            ("AES-256-GCM", "AES", EvaluationResult.PASS),
            ("aes-256-gcm", "AES", EvaluationResult.PASS),  # case-insensitive
            ("RC4", "AES", EvaluationResult.FAIL),
        ],
    )
    def test_contains(self, actual: str, expected: str, result: EvaluationResult) -> None:
        outcome = run(
            [{"fact": "cipher", "operator": "CONTAINS", "expected": expected}],
            [one("cipher", actual, value_type=FactValueType.string)],
        )
        assert outcome.result == result

    def test_exists_passes_when_the_fact_is_present(self) -> None:
        outcome = run([{"fact": "n", "operator": "EXISTS"}], [one("n", "1")])
        assert outcome.result == EvaluationResult.PASS

    def test_exists_fails_when_the_fact_is_absent(self) -> None:
        """EXISTS is one of only two operators that is meaningful with no fact —
        its answer is FAIL, not INSUFFICIENT_EVIDENCE, because absence *is* the
        answer to "is this present"."""
        outcome = run([{"fact": "n", "operator": "EXISTS"}], [])
        assert outcome.result == EvaluationResult.FAIL

    def test_not_exists_passes_when_absent(self) -> None:
        outcome = run([{"fact": "n", "operator": "NOT_EXISTS"}], [])
        assert outcome.result == EvaluationResult.PASS

    def test_not_exists_fails_when_present(self) -> None:
        outcome = run([{"fact": "n", "operator": "NOT_EXISTS"}], [one("n", "1")])
        assert outcome.result == EvaluationResult.FAIL

    def test_booleans_compare_by_equality(self) -> None:
        outcome = run(
            [{"fact": "mfa_enabled", "operator": "==", "expected": True}],
            [one("mfa_enabled", "enabled", value_type=FactValueType.boolean)],
        )
        assert outcome.result == EvaluationResult.PASS

    def test_ordering_a_boolean_is_refused_rather_than_answered(self) -> None:
        """`True > False` is valid Python and meaningless as a compliance
        check. Refusing is safer than returning a confident nonsense answer."""
        with pytest.raises(UnsupportedOperatorError):
            run(
                [{"fact": "mfa_enabled", "operator": ">=", "expected": True}],
                [one("mfa_enabled", "true", value_type=FactValueType.boolean)],
            )

    def test_an_unknown_operator_is_refused(self) -> None:
        with pytest.raises(UnsupportedOperatorError):
            run([{"fact": "n", "operator": "APPROXIMATELY", "expected": 1}], [one("n", "1")])


class TestCoreAcceptanceScenarios:
    """01_REQUIREMENTS.md § Deterministic Rule Evaluation, Acceptance Criteria."""

    def test_length_14_against_at_least_12_passes(self) -> None:
        outcome = run(
            [{"fact": "minimum_password_length", "operator": ">=", "expected": 12}], [one()]
        )
        assert outcome.result == EvaluationResult.PASS

    def test_length_8_against_at_least_12_fails(self) -> None:
        outcome = run(
            [{"fact": "minimum_password_length", "operator": ">=", "expected": 12}],
            [one(value="8")],
        )
        assert outcome.result == EvaluationResult.FAIL

    def test_a_missing_fact_is_insufficient_evidence_not_a_guess(self) -> None:
        outcome = run([{"fact": "minimum_password_length", "operator": ">=", "expected": 12}], [])
        assert outcome.result == EvaluationResult.INSUFFICIENT_EVIDENCE

    def test_two_conflicting_facts_produce_conflict(self) -> None:
        """Never averaged, never arbitrated by preferring a source."""
        outcome = run(
            [{"fact": "mfa_enabled", "operator": "==", "expected": True}],
            [
                one("mfa_enabled", "true", value_type=FactValueType.boolean),
                one("mfa_enabled", "false", value_type=FactValueType.boolean),
            ],
        )
        assert outcome.result == EvaluationResult.CONFLICT
        assert outcome.contradictions[0]["fact"] == "mfa_enabled"

    def test_two_agreeing_facts_are_corroboration_not_conflict(self) -> None:
        """Two documents saying the same thing is the good case. Treating it as
        a conflict would punish clients for providing thorough evidence."""
        outcome = run(
            [{"fact": "minimum_password_length", "operator": ">=", "expected": 12}],
            [one(), one()],
        )
        assert outcome.result == EvaluationResult.PASS
        assert outcome.contradictions == []

    def test_an_unreadable_value_is_insufficient_not_coerced(self) -> None:
        """A value that will not cast to its declared type produces no verdict.
        Coercing "unknown" to 0 would silently turn a missing setting into a
        definite FAIL."""
        outcome = run(
            [{"fact": "n", "operator": ">=", "expected": 12}],
            [one("n", "not-a-number")],
        )
        assert outcome.result == EvaluationResult.INSUFFICIENT_EVIDENCE


class TestRuleCombination:
    """08_TESTING.md: "Rule-combination logic (multiple rules on one control)"."""

    def test_all_rules_passing_passes_the_control(self) -> None:
        outcome = run(
            [
                {"fact": "a", "operator": ">=", "expected": 10},
                {"fact": "b", "operator": "<=", "expected": 10},
            ],
            [one("a", "12"), one("b", "5")],
        )
        assert outcome.result == EvaluationResult.PASS

    def test_any_definite_failure_fails_the_control(self) -> None:
        outcome = run(
            [
                {"fact": "a", "operator": ">=", "expected": 10},
                {"fact": "b", "operator": "<=", "expected": 10},
            ],
            [one("a", "12"), one("b", "50")],
        )
        assert outcome.result == EvaluationResult.FAIL

    def test_a_failure_outranks_an_unknown(self) -> None:
        """One rule definitively fails and another has no evidence. The control
        cannot pass, and FAIL is the more informative of the two truths."""
        outcome = run(
            [
                {"fact": "a", "operator": ">=", "expected": 10},
                {"fact": "b", "operator": "<=", "expected": 10},
            ],
            [one("a", "1")],
        )
        assert outcome.result == EvaluationResult.FAIL

    def test_some_passed_and_some_unknown_is_partial(self) -> None:
        """Genuinely mixed. Reporting INSUFFICIENT_EVIDENCE would understate
        what was shown; PASS would overstate it."""
        outcome = run(
            [
                {"fact": "a", "operator": ">=", "expected": 10},
                {"fact": "b", "operator": "<=", "expected": 10},
            ],
            [one("a", "12")],
        )
        assert outcome.result == EvaluationResult.PARTIAL

    def test_nothing_known_at_all_is_insufficient_evidence(self) -> None:
        outcome = run(
            [
                {"fact": "a", "operator": ">=", "expected": 10},
                {"fact": "b", "operator": "<=", "expected": 10},
            ],
            [],
        )
        assert outcome.result == EvaluationResult.INSUFFICIENT_EVIDENCE

    def test_conflict_outranks_everything(self) -> None:
        """A contradiction means the evidence base for this control is not
        trustworthy, so no verdict on it should be relied on."""
        outcome = run(
            [
                {"fact": "a", "operator": ">=", "expected": 10},
                {"fact": "b", "operator": "<=", "expected": 10},
            ],
            [one("a", "1"), one("b", "5"), one("b", "50")],
        )
        assert outcome.result == EvaluationResult.CONFLICT


class TestFreshness:
    def test_evidence_inside_the_window_is_not_stale(self) -> None:
        outcome = run(
            [{"fact": "n", "operator": ">=", "expected": 12}],
            [one("n", "14", observed_at=NOW - timedelta(days=10))],
            freshness_window_days=90,
        )
        assert outcome.result == EvaluationResult.PASS
        assert outcome.stale is False

    def test_evidence_past_the_window_is_flagged_stale_alongside_the_result(self) -> None:
        """01_REQUIREMENTS.md Edge Cases: the mechanical result still stands, and
        the staleness rides beside it rather than silently auto-passing."""
        outcome = run(
            [{"fact": "n", "operator": ">=", "expected": 12}],
            [one("n", "14", observed_at=NOW - timedelta(days=400))],
            freshness_window_days=90,
        )
        assert outcome.result == EvaluationResult.PASS
        assert outcome.stale is True

    def test_no_window_means_freshness_is_not_assessed(self) -> None:
        outcome = run(
            [{"fact": "n", "operator": ">=", "expected": 12}],
            [one("n", "14", observed_at=NOW - timedelta(days=4000))],
        )
        assert outcome.stale is False

    def test_undated_evidence_is_not_assumed_current(self) -> None:
        """A document that states no date cannot be shown to be fresh — but nor
        can it be shown stale, so the window simply does not fire."""
        outcome = run(
            [{"fact": "n", "operator": ">=", "expected": 12}],
            [one("n", "14", observed_at=None)],
            freshness_window_days=1,
        )
        assert outcome.stale is False


class TestModeRouting:
    def test_human_assisted_controls_are_never_machine_judged(self) -> None:
        """00_PRODUCT.md §5.7: a control that needs interpretation is not
        force-fitted into a mechanical verdict."""
        outcome = run(
            [{"fact": "n", "operator": ">=", "expected": 12}],
            [one("n", "14")],
            evaluation_mode=EvaluationMode.HUMAN_ASSISTED,
        )
        assert outcome.result == EvaluationResult.INSUFFICIENT_EVIDENCE

    def test_a_control_marked_not_applicable_short_circuits(self) -> None:
        outcome = run([{"fact": "n", "operator": ">=", "expected": 12}], [one()], applicable=False)
        assert outcome.result == EvaluationResult.NOT_APPLICABLE

    def test_no_rules_is_not_a_pass(self) -> None:
        """An empty ruleset checks nothing, and "nothing was checked" must never
        read as "everything was satisfied"."""
        outcome = run([], [one()])
        assert outcome.result == EvaluationResult.INSUFFICIENT_EVIDENCE


class TestDeterminism:
    def test_the_same_inputs_always_produce_the_same_result(self) -> None:
        rules = [{"fact": "n", "operator": ">=", "expected": 12}]
        facts = [one("n", "14")]
        results = {run(rules, facts).result for _ in range(20)}
        assert results == {EvaluationResult.PASS}

    def test_freshness_uses_the_injected_clock_not_the_wall_clock(self) -> None:
        """An engine whose output depends on an implicit clock is not
        reproducible, and a re-run of a past audit would not agree with it."""
        fact = one("n", "14", observed_at=datetime(2020, 1, 1, tzinfo=UTC))
        rules = [{"fact": "n", "operator": ">=", "expected": 12}]

        assert run(rules, [fact], freshness_window_days=90, now=NOW).stale is True
        assert (
            run(
                rules,
                [fact],
                freshness_window_days=90,
                now=datetime(2020, 1, 2, tzinfo=UTC),
            ).stale
            is False
        )


class TestZeroLLMDependency:
    """06_ENGINEERING_RULES.md § The Deterministic Core Invariant.

    The single most important property in the codebase: an LLM cannot influence
    a deterministic result, because the engine cannot reach one.
    """

    def test_the_module_imports_no_llm_or_embedding_client(self) -> None:
        """Enforced by parsing the module's actual imports, not by grepping its
        text — the docstring legitimately *names* the modules it must not import,
        and a substring scan would either fail on the prose or be weakened to the
        point of missing a real import (TASK-107)."""
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(rule_engine.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        offenders = [
            name
            for name in imported
            if "llm" in name.lower() or "embedding" in name.lower() or "anthropic" in name.lower()
        ]
        assert not offenders, f"rule_engine.py imports {offenders}"

    def test_no_llm_module_is_reachable_in_the_engines_import_graph(self) -> None:
        """A transitive import would be just as dangerous as a direct one."""
        import sys

        visited: set[str] = set()

        def walk(module_name: str) -> None:
            if module_name in visited:
                return
            visited.add(module_name)
            module = sys.modules.get(module_name)
            if module is None:
                return
            for value in vars(module).values():
                name = getattr(value, "__module__", None) or getattr(value, "__name__", None)
                if isinstance(name, str) and name.startswith("app."):
                    walk(name)

        walk(rule_engine.__name__)
        assert not [m for m in visited if "llm" in m or "embedding" in m]

    def test_the_engine_runs_correctly_with_every_llm_call_raising(self) -> None:
        """00_PRODUCT.md §5.6, LLM-unavailable row: deterministic controls still
        evaluate correctly with the model entirely unreachable."""
        from app.pipelines.embedding import set_embedding_client
        from app.pipelines.llm import set_llm_client

        class Exploding:
            def complete(self, **kwargs: object) -> None:
                raise ConnectionError("the LLM must never be consulted here")

            def embed(self, texts: list[str]) -> None:
                raise ConnectionError("embeddings must never be consulted here")

        set_llm_client(Exploding())  # type: ignore[arg-type]
        set_embedding_client(Exploding())  # type: ignore[arg-type]
        try:
            rules = [{"fact": "minimum_password_length", "operator": ">=", "expected": 12}]
            assert run(rules, [one(value="14")]).result == EvaluationResult.PASS
            assert run(rules, [one(value="8")]).result == EvaluationResult.FAIL
            assert run(rules, []).result == EvaluationResult.INSUFFICIENT_EVIDENCE
        finally:
            set_llm_client(None)
            set_embedding_client(None)
