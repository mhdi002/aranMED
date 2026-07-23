"""Convert Chandra OCR HTML output into clean text for RAG."""
from html.parser import HTMLParser


class _Extractor(HTMLParser):
    BLOCK = {"div", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "section"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.in_table = 0
        self.row = []
        self.cell = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table += 1
        elif tag in ("td", "th") and self.in_table:
            self.in_cell = True
            self.cell = []
        elif tag in ("br",):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "table" and self.in_table:
            self.in_table -= 1
            self.parts.append("\n")
        elif tag in ("td", "th") and self.in_table:
            self.in_cell = False
            self.row.append("".join(self.cell).strip())
        elif tag == "tr" and self.in_table:
            if self.row:
                self.parts.append(" | ".join(self.row) + "\n")
            self.row = []
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)
        else:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _Extractor()
    try:
        p.feed(html)
    except Exception:
        import re
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(p.parts)
    lines = [ln.strip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()
