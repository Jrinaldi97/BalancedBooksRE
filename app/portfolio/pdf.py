"""Portable statement PDF; no native Pango dependency required."""

from io import BytesIO
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
)


def render(report):
    output = BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        output,
        pagesize=letter,
        title="BalancedBooksRE financial statements",
        author="BalancedBooksRE",
        leftMargin=40,
        rightMargin=40,
        topMargin=40,
        bottomMargin=40,
    )
    story = [
        Paragraph("BalancedBooksRE", styles["Title"]),
        Paragraph(escape(report["scope_label"]), styles["Heading2"]),
        Paragraph(escape(report["basis_label"]), styles["Normal"]),
        Paragraph(report["start"] + " through " + report["end"], styles["Normal"]),
        Spacer(1, 12),
    ]
    for limitation in report["limitations"]:
        story.append(Paragraph("Provisional: " + escape(limitation), styles["Normal"]))

    def money(n):
        return f"${n/100:,.2f}"

    def section(title, rows):
        story.append(Spacer(1, 16))
        table = Table(
            [
                [Paragraph(escape(str(name)), styles["Normal"]), money(value)]
                for name, value in rows
            ],
            colWidths=[390, 142],
            repeatRows=0,
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LINEBELOW", (0, -1), (-1, -1), 0.7, colors.HexColor("#174D45")),
                ]
            )
        )
        story.append(KeepTogether([Paragraph(title, styles["Heading2"]), table]))

    section(
        "Income statement",
        [(x["name"], x["amount_cents"]) for x in report["income_statement"]["lines"]]
        + [("Net income", report["income_statement"]["net_income_cents"])],
    )
    bs = report["balance_sheet"]
    section(
        "Balance sheet",
        [(x["name"], x["amount_cents"]) for x in bs["lines"]]
        + [
            ("Accumulated income", bs["accumulated_income_cents"]),
            ("Total assets", bs["assets_cents"]),
            ("Total liabilities", bs["liabilities_cents"]),
            ("Total equity", bs["equity_cents"]),
        ],
    )
    section(
        "Changes in equity",
        [
            (k.replace("_cents", "").replace("_", " ").capitalize(), v)
            for k, v in report["changes_in_equity"].items()
        ],
    )
    cf = report["cash_flows"]
    section(
        "Cash flows",
        [("Beginning cash", cf["beginning_cents"])]
        + [
            (k.replace("_", " ").capitalize(), v)
            for k, v in cf["sections_cents"].items()
        ]
        + [
            ("Net change in cash", cf["net_change_cents"]),
            ("Ending cash", cf["ending_cents"]),
        ],
    )
    section(
        "Statement checks",
        [
            (k.replace("_cents", "").replace("_", " ").capitalize(), v)
            for k, v in report["checks"].items()
        ],
    )
    story.extend(
        [
            Spacer(1, 14),
            Paragraph(
                "Source entries: " + ", ".join(map(str, report["source_event_ids"])),
                styles["Normal"],
            ),
        ]
    )
    for event in cf["noncash_events"]:
        story.append(
            Paragraph("Noncash event " + str(event["event_id"]), styles["Heading3"])
        )
        for ln in event["lines"]:
            story.append(
                Paragraph(
                    escape(ln["name"])
                    + ": debit "
                    + money(ln["debit_cents"])
                    + ", credit "
                    + money(ln["credit_cents"]),
                    styles["Normal"],
                )
            )
    story.extend(
        [
            Spacer(1, 16),
            Paragraph(
                "SlowBooks Pro 2026 — originally created by Trent Von Holten. Independently modified as BalancedBooksRE.",
                styles["Normal"],
            ),
        ]
    )
    doc.build(story)
    return output.getvalue()
