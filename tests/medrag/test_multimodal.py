def test_html_to_text_table():
    from medrag.ingest.html_to_text import html_to_text
    html = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>"
    text = html_to_text(html)
    assert "A" in text and "B" in text


def test_split_questions_persian():
    from medrag.rag.multimodal import split_questions
    text = "1- علائم تب چیست؟\nالف) سردرد\n2- درمان آنژین چیست؟\nب) نیترات"
    qs = split_questions(text)
    assert len(qs) >= 1
