"""Fact extraction with provenance (01_REQUIREMENTS.md § Fact Extraction,
TASK-108).

This is the boundary between "a document exists" and "a machine-checkable claim
exists". Everything downstream — the rule engine, the Evidence Gate, the whole
trust argument — rests on a Fact being something a human can independently
confirm by opening the cited document at the cited location.

**The primary extractor is deterministic on purpose.** A labelled-value scan
(`minimum password length: 14`) is a pattern match over already-extracted text:
no model, no network, reproducible. That is what makes the LLM-unavailable
acceptance test (00_PRODUCT.md §5.6) pass structurally rather than by mocking
something out — with the LLM entirely unreachable, facts still extract and
DETERMINISTIC controls still evaluate correctly.

An LLM may *assist* by locating a candidate value in prose the scanner missed,
which 02_ARCHITECTURE.md §7.6 permits as a bounded extraction-assistance task.
Even then the model's output is not trusted: the candidate is re-verified
against the document at the location it claims, and a candidate that does not
survive that re-read is discarded rather than stored. A model's confidence is
never, by itself, grounds for marking a fact VERIFIED.

Document content is scanned for *values matching a declared fact schema*, never
for instructions. There is no branch in this module that changes behaviour based
on what a document says to do.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.enums import FactValueType, VerificationStatus
from app.pipelines.extraction import ExtractedSection

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "labelled-scan-1.0.0"

# Recognised affirmative/negative words for boolean facts, kept explicit so the
# mapping is auditable rather than buried in a truthiness check.
_TRUE_WORDS = ("true", "yes", "enabled", "enforced", "on", "required", "active")
_FALSE_WORDS = ("false", "no", "disabled", "not enforced", "off", "none", "inactive")

# "as of 2026-01-15", "generated: 2026-01-15", "report date — 2026-01-15"
_OBSERVED_AT = re.compile(
    r"\b(?:as[ _-]?of|generated(?:[ _-]?on)?|report[ _-]?date|snapshot[ _-]?date|"
    r"collected(?:[ _-]?on)?|exported(?:[ _-]?on)?)\b\W{0,4}([0-9]{4}-[0-9]{2}-[0-9]{2})",
    re.IGNORECASE,
)


@dataclass
class ExtractedFact:
    """A candidate fact before it is persisted.

    `page`/`line`/`cell` are what make it checkable. A candidate that cannot
    populate at least one of them is never returned — 01_REQUIREMENTS.md is
    explicit that a fact with no traceable location is not created, and the
    control is instead left short of evidence.
    """

    name: str
    value: str
    value_type: FactValueType
    page: int | None
    line: int | None
    cell: str | None
    observed_at: datetime | None
    verification_status: VerificationStatus
    extractor_version: str = EXTRACTOR_VERSION
    matched_text: str = ""


def extract_facts(
    sections: list[ExtractedSection],
    fact_schema: list[dict[str, str]],
) -> list[ExtractedFact]:
    """Scan already-extracted document sections for the declared facts.

    One fact per (name, location) match. Multiple documents claiming different
    values is not resolved here — both are stored, and the rule engine reports
    CONFLICT. Silently preferring one source is exactly what
    01_REQUIREMENTS.md forbids.
    """
    found: list[ExtractedFact] = []
    document_observed_at = _find_observed_at(sections)

    for spec in fact_schema:
        name = str(spec.get("name", "")).strip()
        if not name:
            continue
        try:
            value_type = FactValueType(str(spec.get("type", "string")))
        except ValueError:
            logger.warning("Unknown fact value_type in control schema: %r", spec.get("type"))
            continue

        pattern = _label_pattern(name)
        for section in sections:
            page, cell = _parse_location(section.location)
            for line_number, raw_line in enumerate(section.text.splitlines(), start=1):
                match = pattern.search(raw_line)
                if not match:
                    continue
                value = _read_value(match.group("value"), value_type)
                if value is None:
                    continue
                # A page-based document cites the page; a sheet cites the cell;
                # otherwise the line within the section. At least one is always
                # set, or we would not be here.
                found.append(
                    ExtractedFact(
                        name=name,
                        value=value,
                        value_type=value_type,
                        page=page,
                        line=line_number if page is None else None,
                        cell=cell,
                        observed_at=document_observed_at,
                        # VERIFIED because the location is checkable, not
                        # because anything was confident about the value.
                        verification_status=VerificationStatus.VERIFIED,
                        matched_text=raw_line.strip()[:500],
                    )
                )

    return found


def recheck(
    sections: list[ExtractedSection],
    *,
    name: str,
    value: str,
    value_type: FactValueType,
    page: int | None,
    line: int | None,
    cell: str | None,
) -> bool:
    """Re-read the cited location and confirm it still yields the stored value.

    This is Evidence Gate check 5. It deliberately re-derives the value from the
    document rather than trusting the stored `EvidenceFact` row — the whole
    point of the check is to catch a stored fact that no longer matches its
    source, whether through file replacement or an extraction bug.
    """
    for section in sections:
        section_page, section_cell = _parse_location(section.location)
        if page is not None and section_page != page:
            continue
        if cell is not None and section_cell != cell:
            continue
        pattern = _label_pattern(name)
        for line_number, raw_line in enumerate(section.text.splitlines(), start=1):
            if line is not None and page is None and line_number != line:
                continue
            match = pattern.search(raw_line)
            if not match:
                continue
            if _read_value(match.group("value"), value_type) == value:
                return True
    return False


def _label_pattern(fact_name: str) -> re.Pattern[str]:
    """Build a matcher for a declared fact name.

    `minimum_password_length` matches "minimum password length",
    "minimum-password-length" and "Minimum Password Length", followed by a
    separator and a value. Word-separator flexibility is the only latitude
    taken — the label itself must be present, so an unrelated number elsewhere
    on the page can never be captured as this fact.
    """
    words = [re.escape(w) for w in re.split(r"[_\s-]+", fact_name.strip()) if w]
    label = r"[\s_-]+".join(words)
    return re.compile(
        rf"\b{label}\b\s*(?:[:=]|\bis\b|\bset\s+to\b)\s*(?P<value>\"[^\"]{{1,120}}\"|'[^']{{1,120}}'|[^\s,;|]{{1,120}})",
        re.IGNORECASE,
    )


def _read_value(raw: str, value_type: FactValueType) -> str | None:
    """Normalise a captured token to the stored representation, or None if it is
    not a usable value of the declared type.

    Returning None (rather than a best guess) is what keeps a mis-scan from
    becoming a fabricated fact: an unreadable value simply produces no Fact, and
    the control resolves to INSUFFICIENT_EVIDENCE.
    """
    text = raw.strip().strip("\"'").strip().rstrip(".,;")
    if not text:
        return None

    if value_type == FactValueType.integer:
        digits = re.match(r"^[+-]?\d+", text)
        return digits.group(0) if digits else None

    if value_type == FactValueType.boolean:
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return "true"
        if lowered in _FALSE_WORDS:
            return "false"
        return None

    if value_type == FactValueType.date:
        stamp = re.match(r"^\d{4}-\d{2}-\d{2}", text)
        return stamp.group(0) if stamp else None

    return text


def _find_observed_at(sections: list[ExtractedSection]) -> datetime | None:
    """Find when the evidence says it was current.

    Freshness is measured on this, not on upload time — a config export dated
    last year is stale evidence no matter how recently it was handed over. When
    a document states no date, freshness simply cannot be assessed and the
    control's window does not fire, rather than the document being assumed
    current.
    """
    for section in sections:
        match = _OBSERVED_AT.search(section.text)
        if match:
            try:
                return datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _parse_location(location: str) -> tuple[int | None, str | None]:
    """Turn an extractor location label back into structured coordinates.

    The extractor emits "page 3", "sheet 'Users'", "table 2", "document body".
    Only a page number and a sheet/cell label are structurally checkable against
    the source document, so only those two are lifted out.
    """
    page_match = re.match(r"^page\s+(\d+)", location, re.IGNORECASE)
    if page_match:
        return int(page_match.group(1)), None
    sheet_match = re.match(r"^sheet\s+'(.+?)'", location, re.IGNORECASE)
    if sheet_match:
        return None, sheet_match.group(1)[:40]
    return None, None
