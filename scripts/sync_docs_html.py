"""Convert docs/*.md to docs/*.html matching the existing style."""

import re
import sys
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent.parent / "docs"

CSS = """\
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,
Arial,sans-serif;font-size:16px;line-height:1.7;
color:#2c2c2c;background:#faf8f5;padding:2rem 1rem}
.container{max-width:740px;margin:0 auto}
h1,h2,h3,h4{font-family:Georgia,"Times New Roman",serif;color:#1a1a1a;line-height:1.3}
h1{font-size:2.2rem;margin-bottom:.25rem;border-bottom:2px solid #e8dcc8;padding-bottom:.5rem}
h2{font-size:1.5rem;margin-top:2.5rem;margin-bottom:.75rem}
h3{font-size:1.2rem;margin-top:1.5rem;margin-bottom:.5rem}
h4{font-size:1.05rem;margin-top:1.2rem;margin-bottom:.4rem}
p{margin-bottom:1rem;color:#444}
a{color:#8b6f4e;text-decoration:none;font-weight:500}
a:hover{text-decoration:underline}
code{background:#f0ebe3;padding:.15em .4em;border-radius:3px;
font-size:.875em;font-family:"SF Mono","Fira Code","Fira Mono",monospace}
pre{background:#f0ebe3;padding:1rem;border-radius:6px;overflow-x:auto;margin-bottom:1rem;font-size:.875em;line-height:1.5}
pre code{background:0 0;padding:0}
table{width:100%;border-collapse:collapse;margin-bottom:1rem;font-size:.925rem}
th,td{padding:.5rem .75rem;text-align:left;border-bottom:1px solid #e0d8cc}
th{background:#f0ebe3;font-weight:600}
ul,ol{margin-bottom:1rem;padding-left:1.5rem}
li{margin-bottom:.25rem}
hr{border:none;border-top:1px solid #e0d8cc;margin:2rem 0}
.header-nav{margin-bottom:2rem;padding-bottom:1rem;border-bottom:1px solid #e0d8cc}
.header-nav a{margin-right:1.2rem;font-size:.9rem}
@media(max-width:600px){body{padding:1rem}h1{font-size:1.8rem}table{font-size:.85rem}}
"""

NAV_LINKS = [
    ("index.html", "Home"),
    ("ARCHITECTURE.html", "Architecture"),
    ("AGENTS.html", "Agents"),
    ("MCP_SERVERS.html", "MCP Servers"),
    ("DESIGN_DECISIONS.html", "Design Decisions"),
    ("API_REFERENCE.html", "API"),
    ("SECURITY.html", "Security"),
    ("CHANGELOG.html", "Changelog"),
    ("TESTS.html", "Tests"),
    ("diagrams/index.html", "Diagrams"),
]


def _title_from_md(md_text: str) -> str:
    m = re.search(r"^#\s+(.+)", md_text)
    return m.group(1).strip() if m else "FinSight"


def convert(md_path: Path) -> str:
    md_text = md_path.read_text(encoding="utf-8")
    title = _title_from_md(md_text)

    # Remove first h1 (it's the title, now in HTML)
    body_md = re.sub(r"^#\s+.+\n?", "", md_text, count=1).strip()

    # Convert markdown to HTML
    html_body = markdown.markdown(
        body_md,
        extensions=["fenced_code", "tables", "codehilite"],
    )

    nav_html = "\n".join(
        f'<a href="{href}">{label}</a>'
        for href, label in NAV_LINKS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FinSight &mdash; {title}</title>
<style>
{CSS}
</style>
</head>
<body>
<div class="container">
<div class="header-nav">
{nav_html}
</div>

{html_body}

<hr>
<p style="font-size:0.85rem;color:#888;margin-top:1rem">
FinSight &mdash; Multi-Agent Investment Research System</p>
</div>
</body>
</html>
"""


def main() -> int:
    md_files = sorted(DOCS.glob("*.md"))
    converted = 0
    for md_path in md_files:
        html_path = md_path.with_suffix(".html")
        html_content = convert(md_path)
        html_path.write_text(html_content, encoding="utf-8")
        print(f"  {md_path.name} -> {html_path.name}")
        converted += 1
    print(f"\nConverted {converted} files ({DOCS})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
