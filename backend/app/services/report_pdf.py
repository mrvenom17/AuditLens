"""Report PDF generation.

01_REQUIREMENTS.md § Engagement Finalization: "Generate the export document
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

_STATUS_LABELS = {
    "satisfied": "Satisfied",
    "partial": "Partially satisfied",
    "not_satisfied": "Not satisfied",
    "not_applicable": "Not applicable",
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
        title=f"PCI DSS Assessment — {snapshot['engagement']['client_name']}",
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

    engagement = snapshot["engagement"]
    story: list[Any] = [
        Paragraph("PCI DSS v4.0.1 Assessment Report", styles["Title"]),
        Paragraph(_escape(engagement["client_name"]), styles["Heading2"]),
        Spacer(1, 6 * mm),
    ]

    meta_rows = [
        ["Framework", snapshot["framework"]],
        ["Entity type", engagement["entity_type"].replace("_", " ")],
        ["Merchant level", engagement["merchant_level"] or "n/a"],
        ["SAQ type", engagement["existing_saq_type"] or "not recorded"],
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
            "AI-suggested values are recorded alongside each finding for audit purposes and are "
            "not determinations.",
            small,
        ),
        PageBreak(),
    ]

    story.append(Paragraph("Findings", heading))
    if not snapshot["findings"]:
        story.append(Paragraph("No approved findings.", body))

    for finding in snapshot["findings"]:
        status = _STATUS_LABELS.get(finding["final_status"] or "", finding["final_status"] or "—")
        block: list[Any] = [
            Paragraph(
                f"<b>{_escape(finding['clause_id'])}</b> — {_escape(finding['title'])}", body
            ),
            Paragraph(f"<b>Assessed status:</b> {_escape(status)}", body),
        ]
        if finding["review_note"]:
            block.append(
                Paragraph(f"<b>Assessor note:</b> {_escape(finding['review_note'])}", body)
            )
        if finding["citations"]:
            locations = ", ".join(_escape(c.get("location", "")) for c in finding["citations"])
            block.append(Paragraph(f"<b>Evidence cited:</b> {locations}", small))
        if finding["ai_suggested_status"]:
            confidence = finding["ai_confidence"]
            block.append(
                Paragraph(
                    f"AI draft suggestion (not a determination): "
                    f"{_escape(_STATUS_LABELS.get(finding['ai_suggested_status'], ''))}"
                    + (f", confidence {confidence:.2f}" if confidence is not None else ""),
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
                            f"<b>{_escape(gap['clause_id'])}</b> — {_escape(gap['title'])}", body
                        ),
                        Paragraph(_escape(gap["gap_note"] or "No reason recorded."), small),
                        Spacer(1, 3 * mm),
                    ]
                )
            )

    document.build(story)
    return buffer.getvalue()
