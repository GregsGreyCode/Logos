"""Every stylesheet's braces balance: one missing '}' silently puts every later rule inside a
phone-only @media block (it happened after a merge on 21 Nov)."""
from pathlib import Path
import re

UI = Path(__file__).resolve().parents[1] / "gateway" / "ui"


def test_every_stylesheet_balances():
    for css in [UI / "app.css", *sorted((UI / "looks").glob("*.css"))]:
        text = re.sub(r"/\*.*?\*/", "", css.read_text(), flags=re.S)
        depth = 0
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("@media"):
                assert depth == 0, f"{css.name}: an @media at line {n} sits inside another block (a '}}' is missing above it)"
            depth += line.count("{") - line.count("}")
            assert depth >= 0, f"{css.name}: an extra '}}' by line {n}"
        assert depth == 0, f"{css.name}: {depth} block(s) never closed"
