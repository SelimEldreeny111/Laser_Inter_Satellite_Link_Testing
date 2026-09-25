"""Render the third-party GUI interface Markdown specification as a PDF."""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    LongTable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "GUI_DEVELOPER_INTERFACE_SPEC.md"
OUTPUT = ROOT / "output" / "pdf" / "Laser_XZ_GUI_Developer_Interface_Specification.pdf"

NAVY = colors.HexColor("#0F172A")
BLUE = colors.HexColor("#1D4ED8")
LIGHT_BLUE = colors.HexColor("#DBEAFE")
SLATE = colors.HexColor("#475569")
LIGHT_SLATE = colors.HexColor("#E2E8F0")
PALE = colors.HexColor("#F8FAFC")


def inline_markup(text: str) -> str:
    """Convert the limited inline Markdown used by this document."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(
        r"`([^`]+)`",
        lambda match: f'<font name="Courier" color="#1D4ED8">{match.group(1)}</font>',
        escaped,
    )
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    return escaped


def make_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}
    styles["cover_title"] = ParagraphStyle(
        "CoverTitle",
        parent=base["Title"],
        fontName="Helvetica-Bold",
        fontSize=27,
        leading=32,
        textColor=NAVY,
        alignment=TA_LEFT,
        spaceAfter=10,
    )
    styles["cover_subtitle"] = ParagraphStyle(
        "CoverSubtitle",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=21,
        textColor=BLUE,
        spaceAfter=18,
    )
    styles["h1"] = ParagraphStyle(
        "H1",
        parent=base["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=NAVY,
        spaceBefore=8,
        spaceAfter=10,
        keepWithNext=True,
    )
    styles["h2"] = ParagraphStyle(
        "H2",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=BLUE,
        spaceBefore=10,
        spaceAfter=7,
        keepWithNext=True,
    )
    styles["h3"] = ParagraphStyle(
        "H3",
        parent=base["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=14,
        textColor=NAVY,
        spaceBefore=8,
        spaceAfter=5,
        keepWithNext=True,
    )
    styles["body"] = ParagraphStyle(
        "Body",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=8.8,
        leading=12.2,
        textColor=NAVY,
        spaceAfter=6,
    )
    styles["cover_meta"] = ParagraphStyle(
        "CoverMeta",
        parent=styles["body"],
        fontSize=10,
        leading=16,
        textColor=SLATE,
        spaceAfter=2,
    )
    styles["bullet"] = ParagraphStyle(
        "Bullet",
        parent=styles["body"],
        leftIndent=14,
        firstLineIndent=-8,
        bulletIndent=4,
        bulletFontName="Helvetica",
        bulletFontSize=8.8,
        spaceAfter=3,
    )
    styles["number"] = ParagraphStyle(
        "Number",
        parent=styles["body"],
        leftIndent=18,
        firstLineIndent=-12,
        bulletFontName="Helvetica",
        bulletFontSize=8.8,
        spaceAfter=3,
    )
    styles["table"] = ParagraphStyle(
        "TableCell",
        parent=styles["body"],
        fontSize=7.1,
        leading=9.2,
        spaceAfter=0,
    )
    styles["table_header"] = ParagraphStyle(
        "TableHeader",
        parent=styles["table"],
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )
    styles["code"] = ParagraphStyle(
        "Code",
        parent=styles["body"],
        fontName="Courier",
        fontSize=6.8,
        leading=9,
        textColor=NAVY,
        wordWrap="CJK",
        spaceAfter=0,
    )
    styles["footer"] = ParagraphStyle(
        "Footer",
        parent=styles["body"],
        fontSize=7,
        textColor=SLATE,
        alignment=TA_CENTER,
    )
    return styles


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table_widths(rows: list[list[str]], total_width: float) -> list[float]:
    columns = len(rows[0])
    weights: list[float] = []
    for column in range(columns):
        longest = max(len(row[column]) if column < len(row) else 0 for row in rows)
        weights.append(float(max(9, min(longest, 44))))
    weight_total = sum(weights)
    return [total_width * weight / weight_total for weight in weights]


def build_table(
    raw_rows: list[list[str]],
    styles: dict[str, ParagraphStyle],
    available_width: float,
) -> LongTable:
    body_rows = [raw_rows[0]] + raw_rows[2:]
    rendered: list[list[Paragraph]] = []
    for row_index, row in enumerate(body_rows):
        style = styles["table_header"] if row_index == 0 else styles["table"]
        rendered.append([Paragraph(inline_markup(cell), style) for cell in row])

    table = LongTable(
        rendered,
        colWidths=table_widths(body_rows, available_width),
        repeatRows=1,
        hAlign="LEFT",
        splitByRow=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BLUE),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def build_code_block(code_lines: list[str], styles: dict[str, ParagraphStyle], width: float) -> Table:
    # Preserve indentation visibly while permitting long STATUS examples to wrap.
    rendered_lines = []
    for line in code_lines:
        escaped = html.escape(line, quote=False).replace(" ", "&#160;")
        rendered_lines.append(escaped or "&#160;")
    paragraph = Paragraph("<br/>".join(rendered_lines), styles["code"])
    block = Table([[paragraph]], colWidths=[width], hAlign="LEFT")
    block.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                ("LINEBEFORE", (0, 0), (0, -1), 2.2, BLUE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return block


def parse_markdown(text: str, styles: dict[str, ParagraphStyle], width: float) -> list[object]:
    lines = text.splitlines()
    story: list[object] = []
    index = 0
    cover = True

    def add_paragraph(parts: list[str], style_name: str = "body") -> None:
        joined = " ".join(part.strip() for part in parts).strip()
        if joined:
            story.append(Paragraph(inline_markup(joined), styles[style_name]))

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped == "<!-- PAGEBREAK -->":
            story.append(PageBreak())
            cover = False
            index += 1
            continue
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```"):
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index].rstrip())
                index += 1
            index += 1
            story.append(build_code_block(code_lines, styles, width))
            story.append(Spacer(1, 5))
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and re.match(
            r"^\s*\|?\s*:?-+", lines[index + 1]
        ):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index])
                index += 1
            raw_rows = [split_table_row(table_line) for table_line in table_lines]
            story.append(build_table(raw_rows, styles, width))
            story.append(Spacer(1, 7))
            continue
        if stripped.startswith("# "):
            style_name = "cover_title" if cover else "h1"
            story.append(Paragraph(inline_markup(stripped[2:]), styles[style_name]))
            index += 1
            continue
        if stripped.startswith("## "):
            style_name = "cover_subtitle" if cover else "h2"
            story.append(Paragraph(inline_markup(stripped[3:]), styles[style_name]))
            index += 1
            continue
        if stripped.startswith("### "):
            story.append(Paragraph(inline_markup(stripped[4:]), styles["h3"]))
            index += 1
            continue
        if stripped.startswith("- "):
            bullet_parts = [stripped[2:]]
            index += 1
            while index < len(lines) and lines[index].startswith(("  ", "\t")):
                bullet_parts.append(lines[index].strip())
                index += 1
            story.append(
                Paragraph(
                    inline_markup(" ".join(bullet_parts)),
                    styles["bullet"],
                    bulletText="-",
                )
            )
            continue
        numbered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if numbered:
            number_parts = [numbered.group(2)]
            index += 1
            while index < len(lines) and lines[index].startswith(("  ", "\t")):
                number_parts.append(lines[index].strip())
                index += 1
            story.append(
                Paragraph(
                    inline_markup(" ".join(number_parts)),
                    styles["number"],
                    bulletText=f"{numbered.group(1)}.",
                )
            )
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines):
            candidate = lines[index].strip()
            if (
                not candidate
                or candidate.startswith("#")
                or candidate.startswith("|")
                or candidate.startswith("```")
                or candidate.startswith("- ")
                or candidate == "<!-- PAGEBREAK -->"
                or re.match(r"^\d+\.\s+", candidate)
            ):
                break
            paragraph_lines.append(candidate)
            index += 1
        add_paragraph(paragraph_lines, "cover_meta" if cover and stripped.startswith("**") else "body")

    return story


class InterfaceSpecDocument(BaseDocTemplate):
    def __init__(self, filename: str) -> None:
        super().__init__(
            filename,
            pagesize=A4,
            rightMargin=16 * mm,
            leftMargin=16 * mm,
            topMargin=18 * mm,
            bottomMargin=17 * mm,
            title="Laser X-Z Controller UART Interface and Motion Control Specification",
            author="Laser Communication Project",
            subject="Firmware 2.4 third-party GUI integration contract",
        )
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="content")
        # Draw headers and footers after flowables so split tables cannot cover them.
        self.addPageTemplates(PageTemplate(id="main", frames=[frame], onPageEnd=self.draw_page))

    def draw_page(self, canvas, document) -> None:  # type: ignore[no-untyped-def]
        canvas.saveState()
        page_width, page_height = A4
        if document.page == 1:
            canvas.setFillColor(BLUE)
            canvas.rect(0, page_height - 12 * mm, page_width, 12 * mm, fill=1, stroke=0)
            canvas.setFillColor(LIGHT_BLUE)
            canvas.rect(0, 0, page_width, 5 * mm, fill=1, stroke=0)
        else:
            canvas.setStrokeColor(colors.HexColor("#CBD5E1"))
            canvas.setLineWidth(0.5)
            canvas.line(16 * mm, page_height - 12 * mm, page_width - 16 * mm, page_height - 12 * mm)
            canvas.setFont("Helvetica-Bold", 7.5)
            canvas.setFillColor(SLATE)
            canvas.drawString(16 * mm, page_height - 9.5 * mm, "LASER X-Z CONTROLLER - GUI INTERFACE SPECIFICATION")
            canvas.drawRightString(page_width - 16 * mm, page_height - 9.5 * mm, "FIRMWARE 2.4")

        canvas.setStrokeColor(colors.HexColor("#CBD5E1"))
        canvas.line(16 * mm, 11 * mm, page_width - 16 * mm, 11 * mm)
        canvas.setFillColor(SLATE)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(16 * mm, 7.5 * mm, "Third-party GUI developer handoff")
        canvas.drawRightString(page_width - 16 * mm, 7.5 * mm, f"Page {document.page}")
        canvas.restoreState()


def main() -> int:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = make_styles()
    document = InterfaceSpecDocument(str(OUTPUT))
    story = parse_markdown(SOURCE.read_text(encoding="utf-8"), styles, document.width)
    document.build(story)
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
