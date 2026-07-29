"""GFM Markdown table parser and alignment utilities."""

_BOX = {
    "tl": "\u250c", "tc": "\u252c", "tr": "\u2510",
    "ml": "\u251c", "mc": "\u253c", "mr": "\u2524",
    "bl": "\u2514", "bc": "\u2534", "br": "\u2518",
    "h": "\u2500",  "v": "\u2502",
}


def parse_markdown_table(lines: list[str]) -> dict:
    """Parse a GFM markdown table from lines.

    Args:
        lines: List of markdown lines (including header, separator, and data rows)

    Returns:
        dict with keys: "headers" (list of str), "align" (list of str), "rows" (list of list of str)
        align values: "left", "center", "right"
    """
    if not lines:
        return {"headers": [], "align": [], "rows": []}

    first = lines[0].strip()
    if first.count("|") < 1 and "  " not in first:
        return {"headers": [], "align": [], "rows": []}

    # Parse header row
    headers = _parse_row(first)

    # Parse separator row for alignment
    align = ["left"] * len(headers)
    if len(lines) > 1:
        separator = lines[1].strip()
        align = _parse_alignment(separator, len(headers))

    if not align:
        return {"headers": [], "align": [], "rows": []}

    rows = []
    for line in lines[2:]:
        line = line.strip()
        if line:
            rows.append(_parse_row(line))

    num_cols = len(headers)
    for row in rows:
        if len(row) != num_cols:
            if len(row) < num_cols:
                row.extend([""] * (num_cols - len(row)))
            else:
                row[:] = row[:num_cols]

    num_cols = len(headers)
    for row in rows:
        if len(row) != num_cols:
            if len(row) < num_cols:
                row.extend([""] * (num_cols - len(row)))
            else:
                row[:] = row[:num_cols]

    return {"headers": headers, "align": align, "rows": rows}


def _parse_row(line: str) -> list[str]:
    """Parse a markdown table row into cells.

    Handles both leading/trailing pipes and extra whitespace.
    """
    # Strip leading/trailing pipes
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]

    cells = line.split("|")
    return [cell.strip() for cell in cells]


def _parse_alignment(separator: str, num_cols: int) -> list[str]:
    separator = separator.strip()
    if separator.startswith("|"):
        separator = separator[1:]
    if separator.endswith("|"):
        separator = separator[:-1]

    parts = separator.split("|")
    align = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        stripped = part.lstrip(":").rstrip(":")
        if not stripped or "-" not in stripped:
            return []
        if part.startswith(":") and part.endswith(":"):
            align.append("center")
        elif part.endswith(":"):
            align.append("right")
        elif part.startswith(":"):
            align.append("left")
        else:
            align.append("left")

    if not align:
        return []

    while len(align) < num_cols:
        align.append("left")
    if len(align) > num_cols:
        align = align[:num_cols]

    return align


def pad_aligned(text: str, width: int, align: str = "left", *, pad_char: str = " ") -> str:
    """Pad text to specified width with alignment.

    Args:
        text: Text to pad
        width: Target width
        align: "left" | "center" | "right"
        pad_char: Character to use for padding (default: space)

    Returns:
        Padded string, truncated if text exceeds width
    """
    if len(text) > width:
        return text[:width]
    if align == "right":
        return pad_char * (width - len(text)) + text
    if align == "center":
        left_pad = (width - len(text)) // 2
        return pad_char * left_pad + text + pad_char * (width - len(text) - left_pad)
    # default: left
    return text + pad_char * (width - len(text))


def _build_border(widths: list[int], left: str, cross: str, right: str) -> str:
    """Build horizontal border line using box drawing characters."""
    parts = [left]
    for i, w in enumerate(widths):
        parts.append(_BOX["h"] * (w + 2))
        if i < len(widths) - 1:
            parts.append(cross)
    parts.append(right)
    return "".join(parts)


def render_table(table: dict, max_width: int = 80) -> str:
    """Render parsed table as Unicode box-drawing string."""
    headers = table["headers"]
    align = table["align"]
    rows = table["rows"]

    if not headers:
        return ""

    col_widths = []
    for i, h in enumerate(headers):
        max_w = len(h)
        for row in rows:
            if i < len(row):
                max_w = max(max_w, len(str(row[i])))
        col_widths.append(max_w)

    total_width = sum(col_widths) + 3 * len(col_widths) + 1
    if total_width > max_width:
        return render_table_vertical(table, max_width)

    lines = []
    lines.append(_build_border(col_widths, _BOX["tl"], _BOX["tc"], _BOX["tr"]))
    cells = []
    for i, h in enumerate(headers):
        cells.append(f" {pad_aligned(h, col_widths[i], 'center')} ")
    lines.append(_BOX["v"] + _BOX["v"].join(cells) + _BOX["v"])
    lines.append(_build_border(col_widths, _BOX["ml"], _BOX["mc"], _BOX["mr"]))
    for row in rows:
        cells = []
        for i in range(len(headers)):
            text = str(row[i]) if i < len(row) else ""
            cells.append(f" {pad_aligned(text, col_widths[i], align[i])} ")
        lines.append(_BOX["v"] + _BOX["v"].join(cells) + _BOX["v"])
    lines.append(_build_border(col_widths, _BOX["bl"], _BOX["bc"], _BOX["br"]))

    return "\n".join(lines)


def render_table_vertical(table: dict, max_width: int = 40) -> str:
    headers = table["headers"]
    rows = table["rows"]

    if not headers:
        return ""

    lines = []
    for row in rows:
        lines.append(f"┌{'─' * (max_width - 2)}┐")
        for i, h in enumerate(headers):
            val = str(row[i]) if i < len(row) else ""
            content = f" {h}: {val}"
            if len(content) > max_width - 2:
                content = content[:max_width - 5] + "..."
            lines.append(f"│{pad_aligned(content, max_width - 2, 'left')}│")
        lines.append(f"└{'─' * (max_width - 2)}┘")
    return "\n".join(lines)


if __name__ == "__main__":
    # Test parse
    t = parse_markdown_table([
        "| Name | Age |",
        "|------|-----|",
        "| Alice | 30 |",
    ])
    assert t["headers"] == ["Name", "Age"]
    assert t["align"] == ["left", "left"]
    assert t["rows"] == [["Alice", "30"]]

    # Test alignment parse
    t2 = parse_markdown_table([
        "| A | B | C |",
        "|:---|:---:|---:|",
        "| 1 | 2 | 3 |",
    ])
    assert t2["align"] == ["left", "center", "right"]

    # Test pad_aligned
    assert pad_aligned("hi", 5) == "hi   "
    assert pad_aligned("hi", 5, "right") == "   hi"
    assert pad_aligned("hi", 5, "center") == " hi  "
    assert pad_aligned("hello", 5) == "hello"
    assert pad_aligned("hello!", 5) == "hello"

    # Test edge cases
    # Trailing pipe
    t3 = parse_markdown_table(["Name | Age", "|---|----|", "| Bob | 25 |"])
    assert t3["headers"] == ["Name", "Age"]
    assert t3["rows"] == [["Bob", "25"]]

    # Empty cells
    t4 = parse_markdown_table(["| A | B |", "|---|----|", "| | |", "| x | |"])
    assert t4["headers"] == ["A", "B"]
    assert t4["rows"] == [["", ""], ["x", ""]]

    # Multi-word headers
    t5 = parse_markdown_table(["| First Name | Last Name |", "|----------|-----------|", "| John | Doe |"])
    assert t5["headers"] == ["First Name", "Last Name"]

    # Only header + separator (no data)
    t6 = parse_markdown_table(["| A | B |", "|---|----|"])
    assert t6["rows"] == []

    def _test_render_basic_2x2():
        table = parse_markdown_table(["|A|B|", "|-|-|", "|1|2|"])
        result = render_table(table)
        expected = (
            "┌───┬───┐\n"
            "│ A │ B │\n"
            "├───┼───┤\n"
            "│ 1 │ 2 │\n"
            "└───┴───┘"
        )
        assert result == expected, f"Expected:\n{expected}\nGot:\n{result}"

    _test_render_basic_2x2()

    def _test_column_width_auto_adjusts():
        table = parse_markdown_table(["|A|LongHeader|", "|-|-|", "|1|2|"])
        result = render_table(table)
        expected = (
            "┌───┬────────────┐\n"
            "│ A │ LongHeader │\n"
            "├───┼────────────┤\n"
            "│ 1 │ 2          │\n"
            "└───┴────────────┘"
        )
        assert result == expected, f"Expected:\n{expected}\nGot:\n{result}"

    _test_column_width_auto_adjusts()

    def _test_centered_alignment():
        table = parse_markdown_table(["|A|B|", "|:-:|---:|", "|1|2|"])
        result = render_table(table)
        expected = (
            "┌───┬───┐\n"
            "│ A │ B │\n"
            "├───┼───┤\n"
            "│ 1 │ 2 │\n"
            "└───┴───┘"
        )
        assert result == expected, f"Expected:\n{expected}\nGot:\n{result}"

    _test_centered_alignment()

    def _test_vertical_format():
        table = parse_markdown_table(["|A|B|", "|-|-|", "|1|2|"])
        result = render_table_vertical(table, max_width=20)
        assert "┌──────────────────┐" in result
        assert "│ A: 1" in result
        assert "│ B: 2" in result
        assert "└──────────────────┘" in result

    _test_vertical_format()

    print("All tests passed!")
