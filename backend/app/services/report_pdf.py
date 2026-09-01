"""Report PDF generation.

01_REQUIREMENTS.md § Audit Finalization: "Generate the export document
(PDF) from the snapshot", local/self-hosted. Rendered from the Report's
`snapshot_data` and nothing else — regenerating an old report must produce the
same document, which it cannot if the renderer goes back to live tables.

reportlab is used rather than an HTML-to-PDF pipeline: it is pure Python with no
browser or system binary behind it, and it never fetches a remote resource, so
there is no SSRF surface in a document built from client-supplied text.
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# The six-state result vocabulary (00_PRODUCT.md §5.5), rendered for a reader
# who is not familiar with the enum names.
_STATUS_LABELS = {
    "PASS": "Pass",
    "FAIL": "Fail",
    "PARTIAL": "Partial / exception",
    "INSUFFICIENT_EVIDENCE": "Insufficient evidence",
    "CONFLICT": "Conflicting evidence",
    "NOT_APPLICABLE": "Not applicable",
}


def _escape(value: object) -> str:
    """Escape text for reportlab's mini-markup.

    Report content includes client-supplied filenames and LLM-generated
    rationale. Both are untrusted with respect to this renderer: an unescaped
    '<' would either corrupt the layout or be interpreted as markup.
    """
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_report_pdf(snapshot: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"PCI DSS Assessment — {snapshot['audit']['client_name']}",
        author="AuditLens",
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body", parent=styles["BodyText"], fontSize=9, leading=12, alignment=TA_LEFT
    )
    small = ParagraphStyle("Small", parent=body, fontSize=8, textColor=colors.HexColor("#555555"))
    heading = ParagraphStyle(
        "H2", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4
    )

    audit = snapshot["audit"]
    story: list[Any] = [
        Paragraph("PCI DSS v4.0.1 Assessment Report", styles["Title"]),
        Paragraph(_escape(audit["client_name"]), styles["Heading2"]),
        Spacer(1, 6 * mm),
    ]

    meta_rows = [
        ["Framework", snapshot["framework"]],
        ["Entity type", audit["entity_type"].replace("_", " ")],
        ["Merchant level", audit["merchant_level"] or "n/a"],
        ["SAQ type", audit["existing_saq_type"] or "not recorded"],
        ["Corpus version", ", ".join(snapshot["corpus_versions"])],
        ["Generated", snapshot["generated_at"]],
        [
            "Signed off by",
            f"{snapshot['generated_by']['name']} ({snapshot['generated_by']['role']})",
        ],
    ]
    meta = Table([[_escape(a), _escape(b)] for a, b in meta_rows], colWidths=[45 * mm, 120 * mm])
    meta.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
            ]
        )
    )
    story += [meta, Spacer(1, 6 * mm)]

    summary = snapshot["summary"]
    story += [
        Paragraph("Summary", heading),
        Paragraph(
            f"{summary['approved_findings']} of {summary['confirmed_requirements']} confirmed "
            f"requirements have an approved finding. "
            f"{summary['acknowledged_gaps']} were finalized as acknowledged gaps. "
            f"{snapshot['rejected_finding_count']} draft finding(s) were rejected during review "
            f"and are excluded from this report.",
            body,
        ),
        Paragraph(
            "Every finding in this report was reviewed and approved by a named human assessor. "
            "Each entry records both the system result — produced mechanically by the rule "
            "engine from provenanced evidence, with no AI involvement — and the assessor's own "
            "decision, which is what this report attests to. Where the two differ, both are "
            "shown. Any AI-generated text is explanatory only and is never a determination.",
            small,
        ),
        PageBreak(),
    ]

    story.append(Paragraph("Findings", heading))
    if not snapshot["findings"]:
        story.append(Paragraph("No approved findings.", body))

    for finding in snapshot["findings"]:
        decision = finding.get("auditor_decision")
        system_result = finding.get("system_result")
        status = _STATUS_LABELS.get(decision or "", decision or "—")
        block: list[Any] = [
            Paragraph(
                f"<b>{_escape(finding['control_id'])}</b> — {_escape(finding['name'])}", body
            ),
            Paragraph(f"<b>Assessor decision:</b> {_escape(status)}", body),
        ]

        # The machine's verdict is printed beside the human's, always — and an
        # override is called out rather than smoothed over, because a reader
        # deserves to see where the assessor departed from the mechanical
        # result (03_DATA_MODEL.md → Report).
        machine = _STATUS_LABELS.get(system_result or "", system_result or "—")
        if finding.get("is_override"):
            block.append(
                Paragraph(
                    f"<b>System result:</b> {_escape(machine)} — "
                    f"<b>overridden by the assessor.</b>",
                    body,
                )
            )
        else:
            block.append(Paragraph(f"<b>System result:</b> {_escape(machine)}", small))

        if finding["review_note"]:
            block.append(
                Paragraph(f"<b>Assessor note:</b> {_escape(finding['review_note'])}", body)
            )

        rules = finding.get("rules_used") or []
        if rules:
            rendered = "; ".join(
                _escape(f"{r.get('fact')} {r.get('operator')} {r.get('expected')}") for r in rules
            )
            block.append(Paragraph(f"<b>Rules applied:</b> {rendered}", small))

        if finding.get("evidence_locations"):
            # The truncated hash is what lets a reader confirm, years later, that
            # the file they are holding is the file that was assessed
            # (00_PRODUCT.md §5.6: "evidence hashes and locations").
            locations = ", ".join(
                _escape(
                    f"{c.get('fact')}={c.get('value')} ({c.get('location')}"
                    + (f", sha256:{str(c['source_hash'])[:12]}" if c.get("source_hash") else "")
                    + ")"
                )
                for c in finding["evidence_locations"]
            )
            block.append(Paragraph(f"<b>Evidence cited:</b> {locations}", small))

        if finding.get("gate_status") and finding["gate_status"] != "VERIFIED":
            # A result the gate could not verify must never look the same as one
            # it could (01_REQUIREMENTS.md § Finding Review, Edge Cases).
            failed = ", ".join(_escape(c) for c in finding.get("gate_checks_failed") or [])
            block.append(
                Paragraph(
                    f"<b>Evidence gate: {_escape(finding['gate_status'])}</b>"
                    + (f" — checks not satisfied: {failed}" if failed else ""),
                    small,
                )
            )

        if finding.get("contradictions"):
            block.append(
                Paragraph(
                    "<b>Conflicting evidence was found and resolved by the assessor.</b>", small
                )
            )

        if finding.get("stale_evidence"):
            block.append(Paragraph("Evidence is past this control's freshness window.", small))

        if finding.get("evidence_strength"):
            block.append(
                Paragraph(
                    f"<b>Evidence strength:</b> {_escape(finding['evidence_strength'])}"
                    + (
                        f" ({_escape(', '.join(finding.get('strength_factors') or []))})"
                        if finding.get("strength_factors")
                        else ""
                    ),
                    small,
                )
            )

        if finding.get("engine_version"):
            block.append(
                Paragraph(
                    f"Determined mechanically by rule engine v"
                    f"{_escape(finding['engine_version'])} (no AI involvement in this result).",
                    small,
                )
            )
        block.append(
            Paragraph(
                f"Reviewed by {_escape(finding['reviewed_by'])} on "
                f"{_escape((finding['reviewed_at'] or '')[:19])}",
                small,
            )
        )
        block.append(Spacer(1, 4 * mm))
        # KeepTogether stops a finding being split across a page break, which
        # would separate a status from the clause it belongs to.
        story.append(KeepTogether(block))

    if snapshot["acknowledged_gaps"]:
        story += [PageBreak(), Paragraph("Acknowledged gaps", heading)]
        story.append(
            Paragraph(
                "The following in-scope requirements were finalized without supporting "
                "evidence, with the reason recorded by the signing Reviewer.",
                small,
            )
        )
        story.append(Spacer(1, 3 * mm))
        for gap in snapshot["acknowledged_gaps"]:
            story.append(
                KeepTogether(
                    [
                        Paragraph(
                            f"<b>{_escape(gap['control_id'])}</b> — {_escape(gap['name'])}", body
                        ),
                        Paragraph(_escape(gap["gap_note"] or "No reason recorded."), small),
                        Spacer(1, 3 * mm),
                    ]
                )
            )

    document.build(story)
    return buffer.getvalue()
