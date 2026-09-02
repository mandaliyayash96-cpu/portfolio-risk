"""
The risk report as a PDF.

PURE, LIKE THE ENGINE NEXT DOOR
-------------------------------
`build_risk_pdf` takes the dict `risk.services.compute_risk` already returns and
gives back bytes. It reads no database, calls no service, imports no Django, and
fetches nothing - so the document can never disagree with the dashboard, because
both render the same dict from the same call. Architecture rule 2 keeps
`risk/engine.py` pure for the same reason; this module is downstream of the
engine and keeps the property.

That also makes it trivially testable: hand it a literal dict, get bytes.

WHY THERE IS NO RUPEE SIGN IN THIS DOCUMENT
-------------------------------------------
reportlab's built-in fonts (Helvetica and friends) are WinAnsi-encoded, and
U+20B9 - the Indian rupee sign - is not in that encoding. Drawing it produces a
black box, not a symbol, and embedding a font that has it would mean shipping a
TTF with the project.

So money is formatted plainly (1,400.50) and the CURRENCY CODE goes in the
column header: "Market value (INR)". That is standard practice in financial
reporting, it works for any base_currency the portfolio might carry, and it
cannot render as a box. `format.js` does the opposite on screen because a
browser has fonts for everything.

MONEY IS PARSED AS DECIMAL
--------------------------
The report carries money as strings (common/MONEY.md keeps it exact in
transit). It is parsed back through Decimal rather than float here, so the
number printed in the PDF is the number that was stored - a float round-trip
would be invisible at two decimal places right up until it wasn't.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

#: The brand mark as it appears in the document. Rendered in three places, all
#: user-visible: the eyebrow above "Risk Report" on page one, the footer rule on
#: every page, and the PDF's `author` metadata. The stacked header therefore
#: reads "Clarisk" over "Risk Report" - the brand, then what the document is.
APP_NAME = "Clarisk"

#: Placeholder for a value the report could not compute - beta with no
#: benchmark stored, or a matrix cell that came back non-finite. An empty cell
#: would read as "zero" to someone skimming; an em dash reads as "not measured".
MISSING = "—"

# ---------------------------------------------------------------------------
# Palette
#
# Print, not screen: this document has one appearance and it is on white paper.
# The hues are the light-theme values from the dashboard (index.css) so a
# printed report and the page it came from look like the same product.
# ---------------------------------------------------------------------------
INK = colors.HexColor("#0f172a")        # --ink
INK_SOFT = colors.HexColor("#334155")   # --ink-soft
INK_MUTED = colors.HexColor("#5c6b81")  # --ink-muted
ACCENT = colors.HexColor("#1d4ed8")     # --accent
BORDER = colors.HexColor("#d8dfe9")     # --border
SUNKEN = colors.HexColor("#f1f5f9")     # --surface-sunken
BAD = colors.HexColor("#b91c1c")        # --bad
WARN_WASH = colors.HexColor("#fffbeb")  # --warn-wash
WARN_BORDER = colors.HexColor("#fde68a")
WARN_INK = colors.HexColor("#92400e")

PAGE_SIZE = A4
MARGIN = 18 * mm

#: Usable width inside the margins. Tables holding Paragraph cells need a real
#: number here - a None column width leaves reportlab to guess how to wrap the
#: text, and it guesses badly.
CONTENT_WIDTH = PAGE_SIZE[0] - 2 * MARGIN


# ---------------------------------------------------------------------------
# Formatting
#
# Every one of these takes whatever the report actually contains - a string, a
# float, or None - and returns something printable. None of them may raise: a
# report that is missing beta must still produce a PDF, and a document that
# 500s because one metric was null is worse than one with a dash in it.
# ---------------------------------------------------------------------------
def _decimal(value) -> Decimal | None:
    """Parse the report's money strings exactly. None when unparseable."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ArithmeticError, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def money(value, places: int = 2) -> str:
    """`"14000.0000"` -> `"14,000.00"`. No currency symbol - see module docstring."""
    parsed = _decimal(value)
    if parsed is None:
        return MISSING
    return f"{parsed:,.{places}f}"


def quantity(value) -> str:
    """
    `"10.000000"` -> `"10"`, `"0.500000"` -> `"0.5"`.

    Trailing zeros on a share count are DecimalField padding, not precision
    anyone needs to read - the same call `format.js` makes on screen.
    """
    parsed = _decimal(value)
    if parsed is None:
        return MISSING
    normalised = parsed.normalize()
    # int(), not the Decimal itself. normalize() moves a whole number into
    # scientific notation (Decimal("10.000000") -> Decimal("1E+1")), and the
    # thousands format spec preserves it - so formatting the Decimal directly
    # prints a share count of "1E+1". Going through int() drops the exponent.
    if normalised == normalised.to_integral_value():
        return f"{int(normalised):,}"
    return f"{normalised:,f}"


def percent(value, places: int = 2) -> str:
    """`0.1815` -> `"18.15%"`. Ratios only, never a currency amount."""
    if value is None:
        return MISSING
    try:
        number = float(value)
    except (TypeError, ValueError):
        return MISSING
    if number != number:  # NaN
        return MISSING
    return f"{number * 100:.{places}f}%"


def ratio(value, places: int = 2) -> str:
    """`0.879` -> `"0.88"`. For unitless figures: beta, Sharpe, HHI."""
    if value is None:
        return MISSING
    try:
        number = float(value)
    except (TypeError, ValueError):
        return MISSING
    if number != number:
        return MISSING
    return f"{number:.{places}f}"


def _trim_timestamp(value) -> str:
    """
    `"2026-08-27 00:00:00"` -> `"2026-08-27"`.

    The engine stringifies a pandas Timestamp, which always carries a midnight
    time component for a daily series. Trimmed here for the same reason
    `format.js` trims it on screen: the engine's output is tested, so the
    cosmetic fix belongs on the presentation side.
    """
    if not isinstance(value, str):
        return MISSING
    return value.split(" ")[0] or MISSING


# ---------------------------------------------------------------------------
# The metric table's contents
#
# Labels and one-line explanations are deliberately WORD FOR WORD the ones in
# frontend/src/components/RiskCards.jsx. A reader who saw the dashboard and
# then opened the PDF must not be told two different things about what Sortino
# measures. If one changes, change both.
#
# Each row is (key, label, formatter, explanation).
# ---------------------------------------------------------------------------
METRIC_ROWS = [
    (
        "annualized_return",
        "Annualised return",
        percent,
        "Compound growth rate over the measured window.",
    ),
    (
        "annualized_volatility",
        "Annualised volatility",
        percent,
        "How much the portfolio swings in a year - bigger means a rougher ride.",
    ),
    (
        "beta",
        "Beta",
        ratio,
        "Move per 1% move in the benchmark. Above 1 amplifies the market.",
    ),
    (
        "sharpe",
        "Sharpe ratio",
        ratio,
        "Return earned per unit of risk taken. Above 1 is considered good.",
    ),
    (
        "sortino",
        "Sortino ratio",
        ratio,
        "Like Sharpe, but only counts downside moves as risk.",
    ),
    (
        "max_drawdown",
        "Max drawdown",
        percent,
        "Worst peak-to-trough fall over the window.",
    ),
    (
        "var_historical",
        "VaR - historical",
        percent,
        "Max expected 1-day loss, from the actual return distribution.",
    ),
    (
        "var_parametric",
        "VaR - parametric",
        percent,
        "The same loss estimated by assuming returns are normally distributed.",
    ),
    (
        "var_montecarlo",
        "VaR - Monte Carlo",
        percent,
        "The same loss estimated by simulating thousands of possible days.",
    ),
    (
        "cvar",
        "CVaR",
        percent,
        "Average loss on the days that are worse than VaR.",
    ),
    (
        "hhi",
        "Concentration (HHI)",
        ratio,
        "How lopsided the holdings are. 1.0 is everything in one stock.",
    ),
    (
        "effective_holdings",
        "Effective holdings",
        ratio,
        "How many equally-weighted positions this portfolio behaves like.",
    ),
]


# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------
class _NumberedCanvas(pdf_canvas.Canvas):
    """
    A canvas that can print "Page 1 of 3".

    The total is not knowable while the first page is being drawn, so pages are
    held as saved states and written out in `save()`, once the count is known.
    This is reportlab's documented idiom for the problem; the alternative is a
    footer that says "Page 1" and makes a missing page invisible.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        width, _height = PAGE_SIZE
        self.setFont("Helvetica", 8)
        self.setFillColor(INK_MUTED)

        self.setStrokeColor(BORDER)
        self.setLineWidth(0.5)
        self.line(MARGIN, 14 * mm, width - MARGIN, 14 * mm)

        self.drawString(MARGIN, 10 * mm, APP_NAME)
        self.drawRightString(
            width - MARGIN, 10 * mm, f"Page {self._pageNumber} of {total}"
        )


def _styles() -> dict:
    """Paragraph styles, derived from reportlab's sample sheet."""
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=sample["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            alignment=TA_LEFT,
            textColor=INK,
            spaceAfter=2,
        ),
        "eyebrow": ParagraphStyle(
            "eyebrow",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=ACCENT,
            spaceAfter=6,
        ),
        "section": ParagraphStyle(
            "section",
            parent=sample["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=INK,
            spaceBefore=14,
            spaceAfter=6,
            # A section heading must never be the last thing on a page with its
            # table overleaf. This is the mechanism for that; KeepTogether is
            # not, because a holdings table longer than a page cannot be kept
            # together with anything.
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "body",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=INK_SOFT,
        ),
        "muted": ParagraphStyle(
            "muted",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=INK_MUTED,
        ),
        "warning": ParagraphStyle(
            "warning",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=WARN_INK,
        ),
        "cell": ParagraphStyle(
            "cell",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10.5,
            textColor=INK_MUTED,
        ),
    }


def _table_style(numeric_columns: tuple[int, ...]) -> TableStyle:
    """
    The one table look used throughout: a tinted header, hairline rules, and
    right-aligned numbers.

    Numbers are right-aligned because a column of figures is read by comparing
    magnitudes, and ragged-right decimals make that impossible.
    """
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), SUNKEN),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_SOFT),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, BORDER),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    for column in numeric_columns:
        commands.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    return TableStyle(commands)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _header_flowables(report: dict, styles: dict, generated_at: datetime) -> list:
    """Title block: what this is, whose it is, and when it was made."""
    portfolio = report.get("portfolio") or {}
    currency = portfolio.get("base_currency") or ""
    name = portfolio.get("name") or "Portfolio"

    facts = [
        ["Portfolio", str(name)],
        ["Base currency", str(currency) or MISSING],
        [
            f"Market value{f' ({currency})' if currency else ''}",
            money(portfolio.get("market_value")),
        ],
        ["Positions", str(len(portfolio.get("holdings") or []))],
        ["Generated", generated_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()],
    ]

    table = Table(facts, colWidths=[38 * mm, None], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
                ("TEXTCOLOR", (0, 0), (0, -1), INK_MUTED),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (1, 0), (1, -1), INK),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
            ]
        )
    )

    return [
        Paragraph(APP_NAME, styles["eyebrow"]),
        Paragraph("Risk Report", styles["title"]),
        Spacer(1, 8),
        table,
    ]


def _window_flowables(report: dict, styles: dict) -> list:
    """
    The measurement window and the benchmark it was measured against.

    Both belong near the top because they qualify every number below: a Sharpe
    over 30 observations and a Sharpe over 250 are not the same claim, and a
    beta with no benchmark is not a beta at all.
    """
    benchmark = report.get("benchmark") or {}
    ticker = benchmark.get("ticker")
    included = benchmark.get("included")

    if not ticker:
        benchmark_text = "None configured - beta could not be measured."
    elif included:
        benchmark_text = f"{ticker} (included)"
    else:
        benchmark_text = (
            f"{ticker} - no overlapping history stored, so beta is not measured."
        )

    params = report.get("params") or {}
    confidence = params.get("confidence")
    confidence_text = (
        f"{float(confidence) * 100:.0f}%" if isinstance(confidence, (int, float)) else MISSING
    )

    rows = [
        ["Observations", f"{report.get('observations', MISSING)} trading days"],
        [
            "Window",
            f"{_trim_timestamp(report.get('start'))} to {_trim_timestamp(report.get('end'))}",
        ],
        ["Benchmark", benchmark_text],
        ["VaR / CVaR confidence", confidence_text],
    ]

    table = Table(rows, colWidths=[38 * mm, None], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
                ("TEXTCOLOR", (0, 0), (0, -1), INK_MUTED),
                ("TEXTCOLOR", (1, 0), (1, -1), INK),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
            ]
        )
    )
    return [Paragraph("Measurement window", styles["section"]), table]


def _holdings_flowables(report: dict, styles: dict) -> list:
    """The positions the numbers were computed from."""
    portfolio = report.get("portfolio") or {}
    holdings = portfolio.get("holdings") or []
    currency = portfolio.get("base_currency") or ""
    suffix = f" ({currency})" if currency else ""

    if not holdings:
        return [
            Paragraph("Holdings", styles["section"]),
            Paragraph("This portfolio holds nothing.", styles["muted"]),
        ]

    rows = [["Ticker", "Quantity", f"Price{suffix}", f"Market value{suffix}", "Weight"]]
    for holding in holdings:
        rows.append(
            [
                str(holding.get("ticker") or MISSING),
                quantity(holding.get("quantity")),
                money(holding.get("price")),
                money(holding.get("market_value")),
                percent(holding.get("weight"), places=1),
            ]
        )

    # The total, so the table reconciles against the market value in the header
    # rather than asking the reader to add five numbers to check.
    rows.append(["Total", "", "", money(portfolio.get("market_value")), "100.0%"])

    table = Table(
        rows,
        colWidths=[34 * mm, 26 * mm, 30 * mm, 38 * mm, 22 * mm],
        hAlign="LEFT",
        repeatRows=1,  # the header repeats if a long portfolio breaks the page
    )
    style = _table_style(numeric_columns=(1, 2, 3, 4))
    style.add("LINEABOVE", (0, -1), (-1, -1), 0.75, BORDER)
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    table.setStyle(style)

    return [Paragraph("Holdings", styles["section"]), table]


def _metrics_flowables(report: dict, styles: dict) -> list:
    """
    Every headline metric, with its value and a plain-English line.

    The explanation sits in the SAME table as the value rather than in a
    separate legend at the back. A legend somewhere else is a legend nobody
    reads: the point is that a non-quant can follow the row they are looking
    at without moving their eyes off it.
    """
    rows = [["Metric", "Value", "What it means"]]
    for key, label, formatter, explanation in METRIC_ROWS:
        rows.append(
            [
                label,
                formatter(report.get(key)),
                Paragraph(explanation, styles["cell"]),
            ]
        )

    table = Table(
        rows,
        colWidths=[42 * mm, 24 * mm, CONTENT_WIDTH - 66 * mm],
        hAlign="LEFT",
        repeatRows=1,
    )
    style = _table_style(numeric_columns=(1,))

    # Losses are red. Only the four rows that ARE losses - colouring every
    # negative number would make a negative beta (a legitimate hedge) look like
    # a problem.
    for index, (key, _label, _formatter, _explanation) in enumerate(METRIC_ROWS, start=1):
        if key in {"max_drawdown", "var_historical", "var_parametric", "var_montecarlo", "cvar"}:
            style.add("TEXTCOLOR", (1, index), (1, index), BAD)
    table.setStyle(style)

    return [Paragraph("Risk metrics", styles["section"]), table]


def _per_asset_flowables(report: dict, styles: dict) -> list:
    """Annualised volatility per holding - which position is the rough one."""
    per_asset = report.get("per_asset_volatility") or {}
    if not per_asset:
        return []

    rows = [["Ticker", "Annualised volatility"]]
    for ticker, value in per_asset.items():
        rows.append([str(ticker), percent(value)])

    table = Table(rows, colWidths=[34 * mm, 40 * mm], hAlign="LEFT", repeatRows=1)
    table.setStyle(_table_style(numeric_columns=(1,)))

    return [Paragraph("Volatility by holding", styles["section"]), table]


def _warnings_flowables(report: dict, styles: dict) -> list:
    """
    Degradations the reader must see.

    On an amber panel rather than in the body text: a warning that a benchmark
    was missing changes how much of this document to believe, and it must not
    read like a footnote.
    """
    warnings = [str(item) for item in (report.get("warnings") or []) if item]
    if not warnings:
        return []

    body = [Paragraph("Warnings", styles["section"])]
    cells = [[Paragraph(f"• {text}", styles["warning"])] for text in warnings]
    table = Table(cells, colWidths=[CONTENT_WIDTH], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), WARN_WASH),
                ("BOX", (0, 0), (-1, -1), 0.5, WARN_BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    body.append(table)
    return body


def _provenance_flowables(report: dict, styles: dict) -> list:
    """How the numbers were produced, in one small paragraph at the end."""
    params = report.get("params") or {}
    bits = []
    if params.get("trading_days"):
        bits.append(f"{params['trading_days']} trading days per year")
    if isinstance(params.get("rf_per_period"), (int, float)):
        annual_rf = params["rf_per_period"] * (params.get("trading_days") or 252)
        bits.append(f"risk-free rate {annual_rf * 100:.2f}% annualised")
    if params.get("n_sims"):
        bits.append(f"{params['n_sims']:,} Monte Carlo simulations")
    if params.get("seed") is not None:
        bits.append(f"seed {params['seed']}")

    text = (
        "Computed from stored daily closes. "
        + ("Parameters: " + ", ".join(bits) + ". " if bits else "")
        + "Prices are as of the last market data refresh, not necessarily this moment."
    )
    return [Spacer(1, 10), Paragraph(text, styles["muted"])]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_risk_pdf(report: dict, *, generated_at: datetime | None = None) -> bytes:
    """
    Render one portfolio's risk report as a PDF.

    Args:
        report: exactly what `risk.services.compute_risk` returns - the engine's
            metrics plus the `portfolio`, `benchmark` and `warnings` keys that
            service adds. Nothing is fetched or recomputed here; every value in
            the document comes from this dict.
        generated_at: the timestamp printed in the header. Defaults to now in
            UTC. Injectable so a test can assert on a fixed string rather than
            on the clock.

    Returns:
        The PDF file as bytes, ready to hand to an HttpResponse.

    Missing or unmeasurable values render as an em dash rather than raising, so
    a portfolio with no benchmark still produces a complete document - see the
    formatters at the top of this module.
    """
    generated_at = generated_at or datetime.now(dt_timezone.utc)
    styles = _styles()
    buffer = io.BytesIO()

    # BaseDocTemplate rather than SimpleDocTemplate: the footer is drawn by the
    # canvas subclass, and this makes the single frame explicit rather than
    # inherited. The bottom margin clears the footer rule at 14mm.
    document = BaseDocTemplate(
        buffer,
        pagesize=PAGE_SIZE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 6 * mm,
        title=f"Risk report - {(report.get('portfolio') or {}).get('name', 'Portfolio')}",
        author=APP_NAME,
        subject="Portfolio risk report",
    )
    frame = Frame(
        document.leftMargin,
        document.bottomMargin,
        document.width,
        document.height,
        id="body",
    )
    document.addPageTemplates([PageTemplate(id="report", frames=[frame])])

    story: list = []
    story += _header_flowables(report, styles, generated_at)
    story += _warnings_flowables(report, styles)
    story += _window_flowables(report, styles)
    story += _holdings_flowables(report, styles)
    story += _metrics_flowables(report, styles)
    story += _per_asset_flowables(report, styles)
    story += _provenance_flowables(report, styles)

    document.build(story, canvasmaker=_NumberedCanvas)
    return buffer.getvalue()
