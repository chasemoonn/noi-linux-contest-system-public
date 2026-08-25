"""Render CSP-style contest Markdown into a validated PDF.

The renderer intentionally supports a small, deterministic Markdown subset.
Untrusted Markdown is escaped before it reaches ReportLab, so generated
statements cannot inject ReportLab ``Paragraph`` markup.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import hashlib
import re
from pathlib import Path
import shutil
import subprocess
from typing import Iterable, Sequence

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


class PdfBuildError(RuntimeError):
    """The paper could not be rendered or failed post-render validation."""


@dataclass(frozen=True)
class MarkdownDocument:
    slug: str
    title: str
    markdown: str
    input_filename: str
    output_filename: str
    time_limit_ms: int
    memory_limit_mb: int


@dataclass(frozen=True)
class FontConfig:
    """Optional portable TTF fonts.

    When no paths are supplied, ReportLab's bundled ``STSong-Light`` CID font
    is used.  This avoids assumptions about Windows or Linux font locations.
    """

    regular_path: str | Path | None = None
    bold_path: str | Path | None = None


@dataclass(frozen=True)
class PdfInspection:
    path: Path
    page_count: int
    byte_size: int
    extracted_text: str


_PAGE_WIDTH, _PAGE_HEIGHT = A4
_FONT_READY: set[tuple[str, str]] = set()


def _font_runs(text: str, cjk_font: str, latin_font: str) -> list[tuple[str, str]]:
    """Split canvas text so CID fonts never shape ASCII as full-width glyphs."""
    runs: list[tuple[str, str]] = []
    for part in re.split(r"([ -~]+)", text):
        if not part:
            continue
        runs.append((latin_font if re.fullmatch(r"[ -~]+", part) else cjk_font, part))
    return runs


def _draw_mixed_string(
    target: canvas.Canvas,
    x: float,
    y: float,
    text: str,
    *,
    cjk_font: str,
    latin_font: str,
    size: float,
    align: str = "left",
) -> None:
    runs = _font_runs(text, cjk_font, latin_font)
    width = sum(pdfmetrics.stringWidth(value, font, size) for font, value in runs)
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    for font, value in runs:
        target.setFont(font, size)
        target.drawString(x, y, value)
        x += pdfmetrics.stringWidth(value, font, size)


def _register_fonts(config: FontConfig | None) -> tuple[str, str]:
    config = config or FontConfig()
    if config.regular_path is None:
        key = ("STSong-Light", "STSong-Light")
        if key not in _FONT_READY:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            _FONT_READY.add(key)
        # UnicodeCIDFont is registered under its PostScript name.
        return "STSong-Light", "STSong-Light"

    regular = Path(config.regular_path)
    bold = Path(config.bold_path) if config.bold_path else regular
    if not regular.is_file() or not bold.is_file():
        raise PdfBuildError("配置的 PDF 字体文件不存在")
    key = (str(regular.resolve()), str(bold.resolve()))
    suffix = hashlib.sha256("\0".join(key).encode("utf-8")).hexdigest()[:16]
    if key not in _FONT_READY:
        regular_name = f"NOI-Regular-{suffix}"
        bold_name = f"NOI-Bold-{suffix}"
        pdfmetrics.registerFont(TTFont(regular_name, str(regular)))
        pdfmetrics.registerFont(TTFont(bold_name, str(bold)))
        _FONT_READY.add(key)
        return regular_name, bold_name
    return f"NOI-Regular-{suffix}", f"NOI-Bold-{suffix}"


class _NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, body_font: str, latin_font: str, **kwargs):
        self._body_font = body_font
        self._latin_font = latin_font
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict] = []

    def showPage(self) -> None:  # noqa: N802 - ReportLab API
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        page_count = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            if self._pageNumber > 1:
                self.saveState()
                self.setFillColor(colors.HexColor("#555555"))
                _draw_mixed_string(
                    self,
                    _PAGE_WIDTH / 2,
                    14 * mm,
                    f"第 {self._pageNumber} 页 / 共 {page_count} 页",
                    cjk_font=self._body_font,
                    latin_font=self._latin_font,
                    size=8.5,
                    align="center",
                )
                self.restoreState()
            super().showPage()
        super().save()


class _CodeBlock(Flowable):
    def __init__(self, text: str, cjk_font: str, latin_font: str = "Courier"):
        super().__init__()
        self.lines = text.rstrip("\n").splitlines() or [""]
        self.cjk_font = cjk_font
        self.latin_font = latin_font
        self.leading = 13.2
        self.padding = 6

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        self.height = self.padding * 2 + self.leading * len(self.lines)
        return self.width, self.height

    def draw(self) -> None:
        self.canv.saveState()
        self.canv.setFillColor(colors.HexColor("#F7F8FA"))
        self.canv.setStrokeColor(colors.HexColor("#3155C6"))
        self.canv.roundRect(0, 0, self.width, self.height, 3, fill=1, stroke=1)
        self.canv.setFillColor(colors.black)
        for index, line in enumerate(self.lines):
            # Keep very long generated lines on the page rather than letting
            # them draw outside the frame. The full source remains in Markdown.
            clipped = line[:150]
            y = self.height - self.padding - 9 - index * self.leading
            _draw_mixed_string(
                self.canv,
                self.padding,
                y,
                clipped,
                cjk_font=self.cjk_font,
                latin_font=self.latin_font,
                size=8.8,
            )
        self.canv.restoreState()


def _latinize_markup(value: str, latin_font: str, latin_bold_font: str) -> str:
    """Apply Latin fonts only to text nodes, never to ReportLab tags."""
    output: list[str] = []
    bold_depth = 0
    explicit_font_depth = 0
    for token in re.split(r"(<[^>]+>)", value):
        if not token:
            continue
        lowered = token.lower()
        if token.startswith("<"):
            output.append(token)
            if lowered == "<b>":
                bold_depth += 1
            elif lowered == "</b>":
                bold_depth = max(0, bold_depth - 1)
            elif lowered.startswith("<font"):
                explicit_font_depth += 1
            elif lowered.startswith("</font"):
                explicit_font_depth = max(0, explicit_font_depth - 1)
            continue
        if explicit_font_depth:
            output.append(token)
            continue
        chosen = latin_bold_font if bold_depth else latin_font
        output.append(
            re.sub(r"([ -~]+)", rf"<font name='{chosen}'>\1</font>", token)
        )
    return "".join(output)


def _plain_markup(text: str, latin_font: str, latin_bold_font: str | None = None) -> str:
    return _latinize_markup(
        escape(text, quote=False), latin_font, latin_bold_font or latin_font
    )


def _inline_markdown(
    text: str,
    body_font: str,
    latin_font: str = "Helvetica",
    latin_bold_font: str = "Helvetica-Bold",
) -> str:
    """Convert a deliberately small inline subset after HTML escaping."""
    value = escape(text, quote=False)
    value = re.sub(r"`([^`]+)`", rf"<font name='Courier'>\1</font>", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", value)
    return _latinize_markup(value, latin_font, latin_bold_font)


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _table_flowable(
    lines: Sequence[str],
    body_font: str,
    bold_font: str,
    latin_font: str,
    latin_bold_font: str,
    small_style: ParagraphStyle,
) -> Table:
    rows: list[list[Paragraph]] = []
    for row_index, line in enumerate(lines):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(
            [
                Paragraph(
                    _inline_markdown(
                        cell,
                        body_font,
                        latin_bold_font if row_index == 0 else latin_font,
                        latin_bold_font,
                    ),
                    small_style,
                )
                for cell in cells
            ]
        )
    columns = max(len(row) for row in rows)
    for row in rows:
        row.extend(Paragraph("", small_style) for _ in range(columns - len(row)))
    table = Table(rows, repeatRows=1, hAlign="CENTER")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#555555")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FA")),
                ("FONTNAME", (0, 0), (-1, 0), bold_font),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _markdown_flowables(
    markdown: str,
    body_font: str,
    bold_font: str,
    latin_font: str,
    latin_bold_font: str,
) -> list[Flowable]:
    sheet = getSampleStyleSheet()
    title = ParagraphStyle(
        "NOIProblemTitle",
        parent=sheet["Title"],
        fontName=bold_font,
        fontSize=17,
        leading=24,
        alignment=TA_CENTER,
        spaceAfter=13,
        wordWrap="CJK",
    )
    section = ParagraphStyle(
        "NOISection",
        parent=sheet["Heading2"],
        fontName=bold_font,
        fontSize=12.5,
        leading=18,
        spaceBefore=10,
        spaceAfter=5,
        keepWithNext=True,
        wordWrap="CJK",
    )
    body = ParagraphStyle(
        "NOIBody",
        parent=sheet["BodyText"],
        fontName=body_font,
        fontSize=10.4,
        leading=17,
        firstLineIndent=20.8,
        alignment=TA_LEFT,
        spaceAfter=4,
        wordWrap="CJK",
    )
    bullet = ParagraphStyle(
        "NOIBullet",
        parent=body,
        firstLineIndent=-12,
        leftIndent=18,
        spaceAfter=2,
    )
    small = ParagraphStyle(
        "NOISmall",
        parent=body,
        firstLineIndent=0,
        fontSize=9.1,
        leading=14,
    )

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[Flowable] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(value.strip() for value in paragraph).strip()
            if text:
                result.append(
                    Paragraph(
                        _inline_markdown(
                            text, body_font, latin_font, latin_bold_font
                        ),
                        body,
                    )
                )
            paragraph.clear()

    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            if index >= len(lines):
                raise PdfBuildError("Markdown 代码块没有结束标记")
            result.append(_CodeBlock("\n".join(code), body_font))
            result.append(Spacer(1, 3))
        elif stripped.startswith("# "):
            flush_paragraph()
            result.append(
                Paragraph(
                    _inline_markdown(
                        stripped[2:], body_font, latin_bold_font, latin_bold_font
                    ),
                    title,
                )
            )
        elif stripped.startswith(("## ", "### ")):
            flush_paragraph()
            heading = stripped.split(" ", 1)[1]
            result.append(
                Paragraph(
                    f"【{_inline_markdown(heading, body_font, latin_bold_font, latin_bold_font)}】",
                    section,
                )
            )
        elif stripped.startswith(("- ", "* ")):
            flush_paragraph()
            result.append(
                Paragraph(
                    f"•&nbsp;&nbsp;{_inline_markdown(stripped[2:], body_font, latin_font, latin_bold_font)}",
                    bullet,
                )
            )
        elif "|" in stripped and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            flush_paragraph()
            table_lines = [stripped]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table_lines.append(lines[index].strip())
                index += 1
            result.append(
                _table_flowable(
                    table_lines,
                    body_font,
                    bold_font,
                    latin_font,
                    latin_bold_font,
                    small,
                )
            )
            result.append(Spacer(1, 4))
            continue
        elif not stripped:
            flush_paragraph()
        else:
            paragraph.append(stripped)
        index += 1
    flush_paragraph()
    return result


def _cover_flowables(
    contest_title: str,
    subtitle: str,
    documents: Sequence[MarkdownDocument],
    body_font: str,
    bold_font: str,
    latin_font: str,
    latin_bold_font: str,
) -> list[Flowable]:
    sheet = getSampleStyleSheet()
    cover_title = ParagraphStyle(
        "NOICoverTitle",
        parent=sheet["Title"],
        fontName=bold_font,
        fontSize=21,
        leading=31,
        alignment=TA_CENTER,
        wordWrap="CJK",
        spaceAfter=7,
    )
    cover_subtitle = ParagraphStyle(
        "NOICoverSubtitle",
        parent=cover_title,
        fontName=body_font,
        fontSize=13,
        leading=21,
        spaceAfter=12,
    )
    cell_style = ParagraphStyle(
        "NOICoverCell",
        parent=sheet["BodyText"],
        fontName=body_font,
        fontSize=8.8,
        leading=12.5,
        alignment=TA_CENTER,
        wordWrap="CJK",
    )
    rows: list[list[Paragraph]] = [
        [
            Paragraph(_plain_markup("题目", latin_bold_font), cell_style),
            Paragraph(_plain_markup("输入文件", latin_bold_font), cell_style),
            Paragraph(_plain_markup("输出文件", latin_bold_font), cell_style),
            Paragraph(_plain_markup("时间", latin_bold_font), cell_style),
            Paragraph(_plain_markup("内存", latin_bold_font), cell_style),
        ]
    ]
    for document in documents:
        rows.append(
            [
                Paragraph(_plain_markup(document.title, latin_font), cell_style),
                Paragraph(_plain_markup(document.input_filename, "Courier"), cell_style),
                Paragraph(_plain_markup(document.output_filename, "Courier"), cell_style),
                Paragraph(
                    _plain_markup(f"{document.time_limit_ms / 1000:g} 秒", latin_font),
                    cell_style,
                ),
                Paragraph(
                    _plain_markup(f"{document.memory_limit_mb} MiB", latin_font),
                    cell_style,
                ),
            ]
        )
    table = Table(
        rows,
        colWidths=[54 * mm, 33 * mm, 33 * mm, 22 * mm, 24 * mm],
        repeatRows=1,
        hAlign="CENTER",
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.55, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF8")),
                ("FONTNAME", (0, 0), (-1, 0), bold_font),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    note_style = ParagraphStyle(
        "NOICoverNotes",
        parent=cell_style,
        alignment=TA_LEFT,
        fontSize=9.8,
        leading=17,
        leftIndent=8 * mm,
        rightIndent=8 * mm,
    )
    return [
        Spacer(1, 22 * mm),
        Paragraph(_plain_markup(contest_title, latin_bold_font), cover_title),
        Paragraph(_plain_markup(subtitle, latin_font), cover_subtitle),
        table,
        Spacer(1, 12 * mm),
        Paragraph(
            _plain_markup("注意事项", latin_bold_font),
            ParagraphStyle("NOINoteTitle", parent=cover_subtitle, fontName=bold_font),
        ),
        Paragraph(
            _plain_markup("1. 程序必须使用题面指定的输入、输出文件。", latin_font),
            note_style,
        ),
        Paragraph(
            _plain_markup("2. 自测数据仅供本地调试，不参与正式评分。", latin_font),
            note_style,
        ),
        Paragraph(
            _plain_markup("3. 以比赛系统记录的截止时间和递交记录为准。", latin_font),
            note_style,
        ),
    ]


def _header_callback(
    contest_title: str, problem_title: str, body_font: str, latin_font: str
):
    def draw_header(target: canvas.Canvas, _doc: BaseDocTemplate) -> None:
        target.saveState()
        target.setFillColor(colors.HexColor("#444444"))
        _draw_mixed_string(
            target,
            24 * mm,
            _PAGE_HEIGHT - 16 * mm,
            contest_title[:42],
            cjk_font=body_font,
            latin_font=latin_font,
            size=8.3,
        )
        _draw_mixed_string(
            target,
            _PAGE_WIDTH - 24 * mm,
            _PAGE_HEIGHT - 16 * mm,
            problem_title[:28],
            cjk_font=body_font,
            latin_font=latin_font,
            size=8.3,
            align="right",
        )
        target.setStrokeColor(colors.HexColor("#777777"))
        target.line(24 * mm, _PAGE_HEIGHT - 18 * mm, _PAGE_WIDTH - 24 * mm, _PAGE_HEIGHT - 18 * mm)
        target.restoreState()

    return draw_header


def inspect_pdf(
    pdf_path: str | Path,
    *,
    required_text: Iterable[str] = (),
    minimum_pages: int = 1,
) -> PdfInspection:
    path = Path(pdf_path)
    if not path.is_file() or path.stat().st_size < 512:
        raise PdfBuildError("生成的 PDF 文件不存在或异常过小")
    try:
        reader = PdfReader(str(path), strict=True)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf exposes several parser exceptions
        raise PdfBuildError(f"生成的 PDF 无法解析: {exc}") from exc
    if len(reader.pages) < minimum_pages:
        raise PdfBuildError(
            f"生成的 PDF 页数不足: {len(reader.pages)} < {minimum_pages}"
        )
    # PDF text extractors may insert line breaks and extra spaces where a
    # Paragraph wrapped visually.  Required text is a content check, not a
    # demand that a long title remain on one physical line.
    normalized_text = re.sub(r"\s+", " ", text).strip()
    missing = [
        needle
        for needle in required_text
        if needle
        and re.sub(r"\s+", " ", str(needle)).strip() not in normalized_text
    ]
    if missing:
        raise PdfBuildError("生成的 PDF 缺少必要文本: " + ", ".join(missing))
    return PdfInspection(path, len(reader.pages), path.stat().st_size, text)


def render_pdf_pages(
    pdf_path: str | Path,
    output_directory: str | Path,
    *,
    dpi: int = 120,
    executable: str | Path | None = None,
) -> list[Path]:
    """Render pages to PNG with Poppler for visual verification."""
    executable = str(executable) if executable else shutil.which("pdftoppm")
    if not executable:
        raise PdfBuildError("未安装 pdftoppm，无法进行 PDF 视觉渲染检查")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    prefix = output / "page"
    completed = subprocess.run(
        [executable, "-png", "-r", str(dpi), str(Path(pdf_path)), str(prefix)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise PdfBuildError(
            "pdftoppm 渲染失败: " + (completed.stderr.strip() or "unknown error")
        )
    pages = sorted(output.glob("page-*.png"))
    if not pages or any(page.stat().st_size == 0 for page in pages):
        raise PdfBuildError("pdftoppm 没有生成有效页面图片")
    return pages


def render_csp_pdf(
    contest_title: str,
    subtitle: str,
    documents: Sequence[MarkdownDocument],
    destination: str | Path,
    *,
    font_config: FontConfig | None = None,
) -> PdfInspection:
    if not contest_title.strip():
        raise PdfBuildError("比赛标题不能为空")
    if not documents:
        raise PdfBuildError("没有题目，无法生成 PDF")
    body_font, bold_font = _register_fonts(font_config)
    cid_fallback = body_font == "STSong-Light"
    latin_font = "Helvetica" if cid_fallback else body_font
    latin_bold_font = "Helvetica-Bold" if cid_fallback else bold_font
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)

    body_frame = Frame(
        24 * mm,
        20 * mm,
        _PAGE_WIDTH - 48 * mm,
        _PAGE_HEIGHT - 42 * mm,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="body",
    )
    cover_frame = Frame(
        17 * mm,
        15 * mm,
        _PAGE_WIDTH - 34 * mm,
        _PAGE_HEIGHT - 28 * mm,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="cover",
    )
    document = BaseDocTemplate(
        str(target),
        pagesize=A4,
        title=contest_title,
        author="NOI Linux Contest Orchestrator",
        creator="ReportLab",
    )
    templates = [PageTemplate(id="cover", frames=[cover_frame])]
    for index, problem in enumerate(documents, 1):
        templates.append(
            PageTemplate(
                id=f"problem-{index}",
                frames=[body_frame],
                onPage=_header_callback(
                    contest_title, problem.title, body_font, latin_font
                ),
            )
        )
    document.addPageTemplates(templates)

    story: list[Flowable] = _cover_flowables(
        contest_title,
        subtitle,
        documents,
        body_font,
        bold_font,
        latin_font,
        latin_bold_font,
    )
    for index, problem in enumerate(documents, 1):
        story.append(NextPageTemplate(f"problem-{index}"))
        story.append(PageBreak())
        story.extend(
            _markdown_flowables(
                problem.markdown,
                body_font,
                bold_font,
                latin_font,
                latin_bold_font,
            )
        )

    def canvas_factory(*args, **kwargs):
        return _NumberedCanvas(
            *args, body_font=body_font, latin_font=latin_font, **kwargs
        )

    try:
        document.build(story, canvasmaker=canvas_factory)
    except Exception as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, PdfBuildError):
            raise
        raise PdfBuildError(f"PDF 排版失败: {exc}") from exc
    return inspect_pdf(
        target,
        required_text=(contest_title,),
        minimum_pages=len(documents) + 1,
    )
