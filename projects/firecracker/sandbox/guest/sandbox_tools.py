"""Baked helpers for the zero-egress python sandbox (ADR agents/044).

Importable because the guest handler puts /opt/sandbox (where this file lands)
on PYTHONPATH. The point is consistency: rather than have each caller improvise
matplotlib styling, render_table produces one polished, predictable table so
every rendered table looks the same and looks good.
"""

from __future__ import annotations


def render_table(headers, rows, title=None, path="table.png"):
    """Render a styled table as a PNG and return the path it was written to.

    headers: sequence of column labels.
    rows: sequence of rows, each a sequence of cell values (stringified).
    title: optional bold caption drawn above the table.
    path: output filename. Keep it relative (the default "table.png") so the
        sandbox collects the file and it comes back to the caller.

    The style is fixed on purpose: a dark header row, zebra body banding,
    padded cells, numeric columns right-aligned, and a white background sized
    to the content.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    headers = [str(h) for h in headers]
    body = [["" if c is None else str(c) for c in row] for row in rows]
    ncols = max(1, len(headers))
    nrows = len(body)

    def _is_num(value):
        value = value.strip().replace(",", "")
        if not value:
            return False
        try:
            float(value)
        except ValueError:
            return False
        return True

    # A column is right-aligned only when every one of its cells is numeric.
    col_align = []
    for c in range(ncols):
        col_vals = [row[c] for row in body if c < len(row)]
        col_align.append(
            "right" if col_vals and all(_is_num(v) for v in col_vals) else "left"
        )

    # Size from content: width by the widest cell per column, height by rows.
    col_chars = []
    for c in range(ncols):
        cells = [headers[c] if c < len(headers) else ""]
        cells += [row[c] if c < len(row) else "" for row in body]
        col_chars.append(max((len(x) for x in cells), default=3))
    fig_w = max(3.0, 0.11 * sum(col_chars) + 0.5 * ncols)
    # Kept close to the drawn table height so "tight" cropping leaves little
    # dead space below the last row.
    fig_h = 0.38 * (nrows + 1) + (0.5 if title else 0.15)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=200)
    ax.axis("off")
    if title:
        ax.set_title(str(title), fontsize=13, fontweight="bold", loc="left", pad=6)

    # "upper center" hugs the table under the title instead of floating it in
    # the vertical middle of the axes (which leaves an awkward gap).
    tbl = ax.table(
        cellText=body if body else [[""] * ncols],
        colLabels=headers,
        cellLoc="left",
        loc="upper center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(11)
    tbl.scale(1, 1.5)

    header_bg = "#2b2d31"
    band_bg = "#f3f4f6"
    edge = "#d5d7db"
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(edge)
        cell.set_linewidth(0.6)
        cell.PAD = 0.05
        if r == 0:
            cell.set_facecolor(header_bg)
            cell.set_text_props(color="white", fontweight="bold", ha="left")
        else:
            if r % 2 == 0:
                cell.set_facecolor(band_bg)
            cell.set_text_props(ha=col_align[c] if c < len(col_align) else "left")

    tbl.auto_set_column_width(col=list(range(ncols)))

    fig.savefig(path, bbox_inches="tight", pad_inches=0.1, dpi=200, facecolor="white")
    plt.close(fig)
    return path
