"""Layout-aware PDF text reconstruction (ported from medrag)."""

FULL_WIDTH_RATIO = 0.6
COLUMN_MARGIN_RATIO = 0.08


def classify_columns(blocks, page_width):
    x_mid = page_width / 2
    margin = page_width * COLUMN_MARGIN_RATIO
    full, left, right = [], [], []
    for b in blocks:
        x0, y0, x1, y1 = b[0], b[1], b[2], b[3]
        width = x1 - x0
        if width > page_width * FULL_WIDTH_RATIO:
            full.append(b)
        elif x1 <= x_mid + margin:
            left.append(b)
        elif x0 >= x_mid - margin:
            right.append(b)
        else:
            full.append(b)
    return full, left, right


def reading_order(blocks, page_width):
    if not blocks:
        return []
    full, left, right = classify_columns(blocks, page_width)
    if not left or not right:
        return sorted(blocks, key=lambda b: (round(b[1], 1), b[0]))
    left.sort(key=lambda b: b[1])
    right.sort(key=lambda b: b[1])
    full.sort(key=lambda b: b[1])
    col_top = min(left[0][1], right[0][1])
    col_bottom = max(left[-1][3], right[-1][3])
    header = [b for b in full if b[1] < col_top]
    footer = [b for b in full if b[1] >= col_bottom]
    mid_span = [b for b in full if b not in header and b not in footer]
    return header + left + right + mid_span + footer


def blocks_to_text(ordered_blocks, text_index=4):
    return "\n".join(b[text_index].strip() for b in ordered_blocks if b[text_index].strip())


def strip_repeated_boilerplate(pages, min_pages=4, ratio_threshold=0.3, max_len=120):
    if len(pages) < min_pages:
        return pages
    from collections import Counter
    line_counts = Counter()
    per_page_lines = []
    for p in pages:
        lines = [l.strip() for l in p.split("\n") if l.strip()]
        per_page_lines.append(lines)
        for l in set(lines):
            if len(l) <= max_len:
                line_counts[l] += 1
    threshold = max(min_pages, int(len(pages) * ratio_threshold))
    boilerplate = {l for l, c in line_counts.items() if c >= threshold}
    if not boilerplate:
        return pages
    return ["\n".join(l for l in lines if l not in boilerplate) for lines in per_page_lines]
