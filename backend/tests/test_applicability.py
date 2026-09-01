"""Applicability engine tests.

The rubric here is narrow and the stakes are asymmetric. A control wrongly marked
IN_SCOPE costs an auditor five minutes of reading. A control wrongly marked
NOT_APPLICABLE disappears from the audit and nobody ever sees it again — there is
no screen on which its absence shows up.

So the tests that matter most are the ones asserting that an *unanswered* profile
question can never exclude a control. `TestUnansweredNeverExcludes` is the file's
reason for existing; everything else is supporting coverage.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.models.enums import ApplicabilityStatus, FactValueType
from app.services import applicability

SERVICE_PROVIDER = [{"fact": "entity_type", "operator": "==", "expected": "service_provider"}]
WIRELESS = [{"fact": "systems.wireless_network", "operator": "==", "expected": True}]
STORES = [{"fact": "stores_cardholder_data", "operator": "==", "expected": True}]


class TestUnansweredNeverExcludes:
    """The safety property. If any of these regress, controls silently vanish
    from audits."""

    def test_a_never_asked_scalar_is_undetermined(self) -> None:
        outcome = applicability.evaluate(STORES, {"entity_type": "merchant"})
        assert outcome.status is ApplicabilityStatus.UNDETERMINED
        assert outcome.unanswered == ["stores_cardholder_data"]

    def test_a_never_asked_set_family_is_undetermined(self) -> None:
        """A set-valued key that was never asked emits no facts at all, rather
        than emitting `false` for every member."""
        outcome = applicability.evaluate(WIRELESS, {"entity_type": "merchant"})
        assert outcome.status is ApplicabilityStatus.UNDETERMINED

    def test_an_explicit_null_counts_as_unanswered(self) -> None:
        outcome = applicability.evaluate(STORES, {"stores_cardholder_data": None})
        assert outcome.status is ApplicabilityStatus.UNDETERMINED

    def test_an_answered_empty_set_is_a_real_no(self) -> None:
        """The counterpart. "We have no systems of these kinds" is an answer, and
        an answer is allowed to exclude a control — otherwise nothing ever
        could."""
        outcome = applicability.evaluate(WIRELESS, {"systems": []})
        assert outcome.status is ApplicabilityStatus.NOT_APPLICABLE

    def test_an_answered_set_without_the_member_is_a_real_no(self) -> None:
        outcome = applicability.evaluate(WIRELESS, {"systems": ["pos_terminals"]})
        assert outcome.status is ApplicabilityStatus.NOT_APPLICABLE

    def test_one_unanswered_condition_undetermines_the_whole_control(self) -> None:
        """Even when another condition definitively fails, an unanswered one
        means we cannot be sure *why* — and PARTIAL/INSUFFICIENT must never
        collapse to NOT_APPLICABLE."""
        conditions = [
            {"fact": "entity_type", "operator": "==", "expected": "service_provider"},
            {"fact": "stores_cardholder_data", "operator": "==", "expected": True},
        ]
        outcome = applicability.evaluate(conditions, {"entity_type": "service_provider"})
        assert outcome.status is ApplicabilityStatus.UNDETERMINED


class TestDeterminations:
    def test_a_matching_condition_puts_the_control_in_scope(self) -> None:
        outcome = applicability.evaluate(SERVICE_PROVIDER, {"entity_type": "service_provider"})
        assert outcome.status is ApplicabilityStatus.IN_SCOPE

    def test_a_failing_condition_excludes_the_control(self) -> None:
        outcome = applicability.evaluate(SERVICE_PROVIDER, {"entity_type": "merchant"})
        assert outcome.status is ApplicabilityStatus.NOT_APPLICABLE

    def test_no_conditions_means_universally_applicable(self) -> None:
        """A control that states no condition applies to everyone. The rule
        engine treats an empty ruleset as INSUFFICIENT_EVIDENCE, which is right
        for evidence and wrong here, so this is short-circuited."""
        assert applicability.evaluate([], {}).status is ApplicabilityStatus.IN_SCOPE

    def test_conditions_are_combined_with_and(self) -> None:
        conditions = [
            {"fact": "entity_type", "operator": "==", "expected": "service_provider"},
            {"fact": "stores_cardholder_data", "operator": "==", "expected": True},
        ]
        both = {"entity_type": "service_provider", "stores_cardholder_data": True}
        one = {"entity_type": "service_provider", "stores_cardholder_data": False}
        assert applicability.evaluate(conditions, both).status is ApplicabilityStatus.IN_SCOPE
        assert applicability.evaluate(conditions, one).status is ApplicabilityStatus.NOT_APPLICABLE

    def test_the_determination_records_its_reasoning(self) -> None:
        """An auditor asking "why is this out of scope?" gets the condition, not
        a bare verdict."""
        outcome = applicability.evaluate(SERVICE_PROVIDER, {"entity_type": "merchant"})
        assert outcome.evidence
        entry = outcome.evidence[0]
        assert entry["fact"] == "entity_type"
        assert entry["actual"] == "merchant"
        assert entry["result"] == "FAIL"


class TestDerivedFacts:
    """OR is expressed as a derived fact rather than as a nested grammar."""

    @pytest.mark.parametrize(
        ("stores", "transmits", "expected"),
        [
            (True, False, ApplicabilityStatus.IN_SCOPE),
            (False, True, ApplicabilityStatus.IN_SCOPE),
            (True, True, ApplicabilityStatus.IN_SCOPE),
            (False, False, ApplicabilityStatus.NOT_APPLICABLE),
        ],
    )
    def test_handles_cardholder_data_is_the_or_of_store_and_transmit(
        self, stores: bool, transmits: bool, expected: ApplicabilityStatus
    ) -> None:
        conditions = [{"fact": "handles_cardholder_data", "operator": "==", "expected": True}]
        profile = {"stores_cardholder_data": stores, "transmits_cardholder_data": transmits}
        assert applicability.evaluate(conditions, profile).status is expected

    def test_the_derived_fact_is_absent_when_neither_input_is_answered(self) -> None:
        conditions = [{"fact": "handles_cardholder_data", "operator": "==", "expected": True}]
        assert applicability.evaluate(conditions, {}).status is ApplicabilityStatus.UNDETERMINED


class TestFlattener:
    def test_set_members_become_individually_named_booleans(self) -> None:
        facts = {f.name: f.value for f in applicability.profile_facts({"data_types": ["pan"]})}
        assert facts["data_types.pan"] == "true"
        assert facts["data_types.cardholder_name"] == "false"

    def test_dotted_names_avoid_prefix_collisions(self) -> None:
        """A joined string plus CONTAINS would match `aws` inside a longer
        provider name. One boolean per member cannot."""
        facts = {
            f.name: f.value for f in applicability.profile_facts({"cloud_providers": ["azure"]})
        }
        assert facts["cloud_providers.aws"] == "false"
        assert facts["cloud_providers.azure"] == "true"

    def test_scalars_keep_their_types(self) -> None:
        by_name = {
            f.name: f
            for f in applicability.profile_facts(
                {"stores_cardholder_data": True, "industry": "saas"}
            )
        }
        assert by_name["stores_cardholder_data"].value_type is FactValueType.boolean
        assert by_name["industry"].value_type is FactValueType.string


class TestAuthoringValidation:
    def test_presence_operators_are_refused(self) -> None:
        """EXISTS/NOT_EXISTS answer PASS/FAIL for a missing fact, so a condition
        using one could never report UNDETERMINED — it would convert an
        unanswered question into an exclusion."""
        for operator in ("EXISTS", "NOT_EXISTS"):
            errors = applicability.validate_conditions([{"fact": "industry", "operator": operator}])
            assert any("not allowed" in e for e in errors)

    def test_an_unknown_attribute_is_refused(self) -> None:
        """A typo would otherwise resolve UNDETERMINED forever and be
        indistinguishable from an unanswered question."""
        errors = applicability.validate_conditions(
            [{"fact": "industy", "operator": "==", "expected": "saas"}]
        )
        assert any("unknown profile attribute" in e for e in errors)

    def test_an_unknown_operator_is_refused(self) -> None:
        errors = applicability.validate_conditions(
            [{"fact": "industry", "operator": "APPROXIMATELY", "expected": "saas"}]
        )
        assert any("unknown operator" in e for e in errors)

    def test_a_well_formed_condition_passes(self) -> None:
        assert (
            applicability.validate_conditions(
                [{"fact": "systems.wireless_network", "operator": "==", "expected": True}]
            )
            == []
        )


class TestZeroLLMDependency:
    def test_the_module_imports_no_llm_or_embedding_client(self) -> None:
        """Applicability decides whether a requirement is assessed at all. A
        model must have no part in that, and the boundary is asserted rather
        than trusted."""
        tree = ast.parse(pathlib.Path(applicability.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not [
            name
            for name in imported
            if "llm" in name.lower() or "embedding" in name.lower() or "anthropic" in name.lower()
        ]

    def test_determinations_are_reproducible(self) -> None:
        results = {
            applicability.evaluate(SERVICE_PROVIDER, {"entity_type": "merchant"}).status
            for _ in range(20)
        }
        assert results == {ApplicabilityStatus.NOT_APPLICABLE}
