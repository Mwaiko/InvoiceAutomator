"""
app/services/etims_dev_pdf.py

Development-mode eTIMS receipt PDF generator.

When APP_ENV=development, instead of submitting to the KRA portal this module
renders a beautifully formatted PDF that mirrors exactly what would be sent,
so developers can verify the payload visually without touching KRA.

Usage (called automatically by etims_tasks.submit_to_etims):
    from app.services.etims_dev_pdf import render_dev_receipt_pdf
    pdf_path = render_dev_receipt_pdf(receipt_header, out_dir="./dev_receipts")
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── Brand / colour palette ────────────────────────────────────────────────────
KRA_GREEN       = colors.HexColor("#006633")   # KRA official green
KRA_LIGHT_GREEN = colors.HexColor("#e8f5ee")   # subtle tint for header bg
KRA_GOLD        = colors.HexColor("#c8960c")   # accent / totals bar
DEV_ORANGE      = colors.HexColor("#e65c00")   # "DEVELOPMENT PREVIEW" banner
ROW_ALT         = colors.HexColor("#f5faf7")   # alternating row tint
BORDER_GREY     = colors.HexColor("#cccccc")
TEXT_DARK       = colors.HexColor("#1a1a1a")
TEXT_MID        = colors.HexColor("#555555")
TEXT_LIGHT      = colors.HexColor("#888888")

PAGE_W, PAGE_H  = A4
MARGIN          = 18 * mm


# ── Styles ────────────────────────────────────────────────────────────────────

def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "dev_banner": ParagraphStyle(
            "dev_banner",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=colors.white,
            alignment=1,          # centre
            spaceAfter=0,
        ),
        "kra_title": ParagraphStyle(
            "kra_title",
            fontName="Helvetica-Bold",
            fontSize=16,
            textColor=KRA_GREEN,
            alignment=1,
            spaceBefore=4,
            spaceAfter=2,
        ),
        "kra_subtitle": ParagraphStyle(
            "kra_subtitle",
            fontName="Helvetica",
            fontSize=9,
            textColor=TEXT_MID,
            alignment=1,
            spaceAfter=8,
        ),
        "section_heading": ParagraphStyle(
            "section_heading",
            fontName="Helvetica-Bold",
            fontSize=8.5,
            textColor=colors.white,
            spaceAfter=0,
            spaceBefore=0,
        ),
        "field_label": ParagraphStyle(
            "field_label",
            fontName="Helvetica-Bold",
            fontSize=8,
            textColor=TEXT_MID,
        ),
        "field_value": ParagraphStyle(
            "field_value",
            fontName="Helvetica",
            fontSize=8.5,
            textColor=TEXT_DARK,
        ),
        "remark": ParagraphStyle(
            "remark",
            fontName="Helvetica-Oblique",
            fontSize=7.5,
            textColor=TEXT_MID,
        ),
        "footer": ParagraphStyle(
            "footer",
            fontName="Helvetica",
            fontSize=7,
            textColor=TEXT_LIGHT,
            alignment=1,
        ),
        "total_label": ParagraphStyle(
            "total_label",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=colors.white,
        ),
        "total_value": ParagraphStyle(
            "total_value",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=KRA_GOLD,
        ),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kv_table(pairs: list[tuple[str, str]], styles: dict, col_widths=None) -> Table:
    """
    Render a two-column label / value table with a hairline border.
    `pairs` is a list of (label, value) tuples.
    """
    usable = PAGE_W - 2 * MARGIN
    if col_widths is None:
        col_widths = [usable * 0.32, usable * 0.68]

    data = [
        [
            Paragraph(label, styles["field_label"]),
            Paragraph(str(value) if value is not None else "—", styles["field_value"]),
        ]
        for label, value in pairs
    ]

    t = Table(data, colWidths=col_widths, repeatRows=0)
    t.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [colors.white, ROW_ALT]),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.25, BORDER_GREY),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _section_header(text: str, styles: dict) -> Table:
    """A full-width green header bar above a section."""
    usable = PAGE_W - 2 * MARGIN
    t = Table(
        [[Paragraph(text, styles["section_heading"])]],
        colWidths=[usable],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), KRA_GREEN),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
    ]))
    return t


def _items_table(items, styles: dict) -> Table:
    """Line-items table with header row and alternating row shading."""
    usable = PAGE_W - 2 * MARGIN
    col_w  = [usable * f for f in (0.34, 0.07, 0.08, 0.10, 0.10, 0.09, 0.10, 0.12)]

    header_style = ParagraphStyle(
        "th", fontName="Helvetica-Bold", fontSize=7.5, textColor=colors.white,
    )
    cell_style = ParagraphStyle(
        "td", fontName="Helvetica", fontSize=7.5, textColor=TEXT_DARK,
    )
    num_style = ParagraphStyle(
        "td_num", fontName="Helvetica", fontSize=7.5, textColor=TEXT_DARK, alignment=2,
    )

    headers = ["Description", "UOM", "Qty", "Unit Price", "Discount", "Supply Amt", "Tax Amt", "Total"]
    data    = [[Paragraph(h, header_style) for h in headers]]

    for item in items:
        data.append([
            Paragraph(item.item_nm or "—",       cell_style),
            Paragraph(item.qty_unit_cd,           cell_style),
            Paragraph(f"{item.qty:,.3f}",         num_style),
            Paragraph(f"{item.prc:,.2f}",         num_style),
            Paragraph(f"{item.dc_rt:.1f}%",       num_style),
            Paragraph(f"{item.sply_amt:,.2f}",    num_style),
            Paragraph(f"{item.tax_amt:,.2f}",     num_style),
            Paragraph(f"{item.tot_amt:,.2f}",     num_style),
        ])

    t = Table(data, colWidths=col_w, repeatRows=1)

    row_count = len(data)
    bg_commands = [("BACKGROUND", (0, 0), (-1, 0), KRA_GREEN)]
    for r in range(1, row_count):
        bg = ROW_ALT if r % 2 == 0 else colors.white
        bg_commands.append(("BACKGROUND", (0, r), (-1, r), bg))

    t.setStyle(TableStyle([
        *bg_commands,
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.25, BORDER_GREY),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (2, 1), (-1, -1), "RIGHT"),
    ]))
    return t


def _totals_table(header, styles: dict) -> Table:
    """Grand-total bar in KRA gold-on-green."""
    usable = PAGE_W - 2 * MARGIN
    lw, rw = usable * 0.60, usable * 0.40

    tax_codes = {i.tax_ty_cd for i in header.items}
    breakdown_rows: list = []
    for code in sorted(tax_codes):
        rate  = header.tax_rate_for(code)
        label = f"Tax ({code} — {rate:.0f}%)" if rate else f"Tax ({code} — exempt)"
        breakdown_rows.append([
            Paragraph(label,                                     styles["total_label"]),
            Paragraph(f"KES {header.tax_by_code(code):>12,.2f}", styles["total_value"]),
        ])

    rows = [
        [Paragraph("Total Supply Amount",  styles["total_label"]),
         Paragraph(f"KES {header.tot_sply_amt:>12,.2f}", styles["total_value"])],
        [Paragraph("Total Taxable Amount", styles["total_label"]),
         Paragraph(f"KES {header.tot_taxbl_amt:>12,.2f}", styles["total_value"])],
        *breakdown_rows,
        [Paragraph("GRAND TOTAL",          ParagraphStyle(
            "gt", fontName="Helvetica-Bold", fontSize=11, textColor=KRA_GOLD)),
         Paragraph(f"KES {header.sum_tot_amt:>12,.2f}", ParagraphStyle(
            "gtv", fontName="Helvetica-Bold", fontSize=11, textColor=KRA_GOLD))],
    ]

    t = Table(rows, colWidths=[lw, rw])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), KRA_GREEN),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("LINEABOVE",     (0, -1), (-1, -1), 0.5, KRA_GOLD),
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


# ── Main public function ──────────────────────────────────────────────────────

def render_dev_receipt_pdf(
    receipt_header,          # fill_kra.ReceiptHeader instance
    out_dir: str | Path = "./dev_receipts",
    filename: str | None = None,
) -> str:
    """
    Render a development-preview PDF for the given ReceiptHeader and return
    the absolute path of the saved file.

    Args:
        receipt_header: a filled fill_kra.ReceiptHeader instance.
        out_dir:        directory where the PDF is written (created if absent).
        filename:       override the auto-generated filename.

    Returns:
        Absolute path (str) to the written PDF file.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if filename is None:
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_inv = (receipt_header.invoice_no or "noref").replace("/", "-")
        filename = f"etims_dev_{safe_inv}_{ts}.pdf"

    pdf_file = str(out_path / filename)
    s        = _styles()

    doc = SimpleDocTemplate(
        pdf_file,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 8 * mm,
    )

    story: list = []

    # ── DEV BANNER ────────────────────────────────────────────────────────────
    usable = PAGE_W - 2 * MARGIN
    banner = Table(
        [[Paragraph("⚠  DEVELOPMENT PREVIEW — NOT A VALID KRA RECEIPT  ⚠", s["dev_banner"])]],
        colWidths=[usable],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), DEV_ORANGE),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(banner)
    story.append(Spacer(1, 6 * mm))

    # ── KRA HEADER ────────────────────────────────────────────────────────────
    story.append(Paragraph("Kenya Revenue Authority — eTIMS", s["kra_title"]))
    story.append(Paragraph(
        "Electronic Tax Invoice Management System  |  Sales Receipt Submission Preview",
        s["kra_subtitle"],
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=KRA_GREEN))
    story.append(Spacer(1, 4 * mm))

    # ── META INFO ROW (invoice no + generated at) ─────────────────────────────
    meta_left  = [
        ("Invoice No.",       receipt_header.invoice_no or "—"),
        ("Store No.",         receipt_header.store_no or "—"),
        ("Payment Type Code", receipt_header.pmt_ty_cd),
    ]
    meta_right = [
        ("Generated At", datetime.now().strftime("%d %b %Y  %H:%M:%S")),
        ("Environment",  "DEVELOPMENT"),
        ("Portal URL",   "etims.kra.go.ke  (not submitted)"),
    ]

    mw = usable / 2 - 2 * mm
    meta_tbl = Table(
        [[_kv_table(meta_left, s, [mw * 0.40, mw * 0.60]),
          _kv_table(meta_right, s, [mw * 0.40, mw * 0.60])]],
        colWidths=[mw + 2 * mm, mw + 2 * mm],
    )
    meta_tbl.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── BUYER / CUSTOMER ─────────────────────────────────────────────────────
    story.append(_section_header("BUYER INFORMATION", s))
    story.append(_kv_table([
        ("Customer Name",        receipt_header.cust_nm),
        ("Customer TIN",         receipt_header.cust_tin or "—"),
        ("Branch Name",          receipt_header.cust_branch_nm or "—"),
        ("Mobile No.",           receipt_header.cust_mbl_no),
        ("Foreign Mobile No.",   receipt_header.cust_mbl_forn_no or "—"),
    ], s))
    story.append(Spacer(1, 4 * mm))

    # ── NON-FISCAL REFERENCES ────────────────────────────────────────────────
    story.append(_section_header("NON-FISCAL REFERENCES", s))
    story.append(_kv_table([
        ("LPO / Order No.",      receipt_header.order_no or "—"),
        ("Delivery Note No.",    receipt_header.delivery_note_no or "—"),
        ("GRN No.",              receipt_header.grn_no or "—"),
        ("Invoice No.",          receipt_header.invoice_no or "—"),
        ("Store No.",            receipt_header.store_no or "—"),
    ], s))
    story.append(Spacer(1, 3 * mm))

    # Remark string (as built from ReceiptHeader.remark)
    story.append(Paragraph(
        f"<b>Portal remark string:</b>  {receipt_header.remark}",
        s["remark"],
    ))
    story.append(Spacer(1, 5 * mm))

    # ── LINE ITEMS ────────────────────────────────────────────────────────────
    story.append(_section_header(f"LINE ITEMS  ({len(receipt_header.items)} item(s))", s))
    story.append(_items_table(receipt_header.items, s))
    story.append(Spacer(1, 5 * mm))

    # ── TOTALS ────────────────────────────────────────────────────────────────
    story.append(_section_header("RECEIPT TOTALS", s))
    story.append(_totals_table(receipt_header, s))
    story.append(Spacer(1, 8 * mm))

    # ── FOOTER ────────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GREY))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "This document is a <b>development-environment simulation</b> of a KRA eTIMS submission. "
        "It has <b>NOT</b> been transmitted to the KRA portal and carries no legal or fiscal validity. "
        f"Generated by eTIMS Dev Renderer on {datetime.now().strftime('%d %b %Y at %H:%M:%S')}.",
        s["footer"],
    ))

    doc.build(story)
    return pdf_file