#!/usr/bin/env python3
"""Convert project Markdown files to themed HTML matching the FinSight docs design.

All output goes into docs/ so every HTML page lives in a single folder.
Each page includes navigation links and a diagrams sidebar linking to all
diagram pages in docs/diagrams/.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# Navigation bar — all hrefs are relative to docs/
NAV_LINKS = [
    ("index.html", "Home"),
    ("README.html", "Overview"),
    ("ARCHITECTURE.html", "Architecture"),
    ("AGENTS.html", "Agents"),
    ("MCP_SERVERS.html", "MCP Servers"),
    ("DESIGN_DECISIONS.html", "Design Decisions"),
    ("API_REFERENCE.html", "API"),
    ("SECURITY.html", "Security"),
    ("CHANGELOG.html", "Changelog"),
    ("TESTS.html", "Tests"),
    ("FRONTEND.html", "Frontend"),
    ("diagrams/index.html", "Diagrams"),
]

# Source .md → destination .html (all outputs land in docs/)
SOURCES: list[tuple[Path, Path]] = [
    (ROOT / "README.md", DOCS / "README.html"),
    (DOCS / "AGENTS.md", DOCS / "AGENTS.html"),
    (DOCS / "API_REFERENCE.md", DOCS / "API_REFERENCE.html"),
    (DOCS / "ARCHITECTURE.md", DOCS / "ARCHITECTURE.html"),
    (DOCS / "CHANGELOG.md", DOCS / "CHANGELOG.html"),
    (DOCS / "DESIGN_DECISIONS.md", DOCS / "DESIGN_DECISIONS.html"),
    (DOCS / "MCP_SERVERS.md", DOCS / "MCP_SERVERS.html"),
    (DOCS / "SECURITY.md", DOCS / "SECURITY.html"),
    (DOCS / "TESTS.md", DOCS / "TESTS.html"),
    (ROOT / "src" / "web" / "nextjs-app" / "README.md", DOCS / "FRONTEND.html"),
]

# Diagram pages (label, href relative to docs/)
DIAGRAM_LINKS = [
    ("Context (C4 L1)", "diagrams/context.html"),
    ("Container (C4 L2)", "diagrams/container.html"),
    ("Component (C4 L3)", "diagrams/component.html"),
    ("Orchestrator", "diagrams/component-orch.html"),
    ("Agents", "diagrams/component-agents.html"),
    ("Infra", "diagrams/component-infra.html"),
    ("Code (C4 L4)", "diagrams/code.html"),
    ("Sequence", "diagrams/sequence.html"),
    ("Phase 1: Input", "diagrams/sequence-phase1.html"),
    ("Phase 2: Dispatch", "diagrams/sequence-phase2.html"),
    ("Phase 3: Synthesis", "diagrams/sequence-phase3.html"),
    ("Data Flow", "diagrams/dataflow.html"),
    ("Input Pipeline", "diagrams/dataflow-input.html"),
    ("Agent Processing", "diagrams/dataflow-processing.html"),
    ("Entity-Relationship", "diagrams/er.html"),
]

STYLE = """\
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:16px;line-height:1.7;color:#2c2c2c;background:#faf8f5;padding:2rem 1rem}
.container{max-width:740px;margin:0 auto}
h1,h2,h3,h4,h5,h6{font-family:Georgia,"Times New Roman",serif;color:#1a1a1a;line-height:1.3}
h1{font-size:2.2rem;margin-bottom:0.25rem;border-bottom:2px solid #e8dcc8;padding-bottom:0.5rem}
h2{font-size:1.5rem;margin-top:2.5rem;margin-bottom:0.75rem;border-bottom:1px solid #e8dcc8;padding-bottom:0.3rem}
h3{font-size:1.2rem;margin-top:1.5rem;margin-bottom:0.5rem}
h4{font-size:1.05rem;margin-top:1.25rem;margin-bottom:0.4rem}
p{margin-bottom:1rem;color:#444}
a{color:#8b6f4e;text-decoration:none;font-weight:500}
a:hover{text-decoration:underline}
ul,ol{margin-bottom:1rem;padding-left:1.5rem;color:#444}
li{margin-bottom:0.25rem}
li>ul,li>ol{margin-bottom:0}
code{background:#f0ebe3;padding:0.15em 0.4em;border-radius:3px;font-size:0.875em;font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace}
pre{background:#1a1a1a;color:#e8dcc8;padding:1rem 1.25rem;border-radius:8px;overflow-x:auto;margin-bottom:1rem;line-height:1.5;font-size:0.85rem}
pre code{background:none;padding:0;color:inherit;font-size:inherit}
table{width:100%;border-collapse:collapse;margin-bottom:1rem;font-size:0.9rem}
th,td{padding:0.5rem 0.75rem;text-align:left;border-bottom:1px solid #e0d8cc}
th{background:#f0ebe3;font-weight:600}
blockquote{border-left:3px solid #e8dcc8;margin:0 0 1rem 0;padding:0.5rem 1rem;color:#666;background:#fff}
hr{border:none;border-top:1px solid #e0d8cc;margin:1.5rem 0}
img{max-width:100%;height:auto}
.header-nav{margin-bottom:2rem;padding-bottom:1rem;border-bottom:1px solid #e0d8cc}
.header-nav a{margin-right:1.2rem;font-size:0.9rem}
.header-nav a.active{color:#1a1a1a;font-weight:700;border-bottom:2px solid #8b6f4e}
.diagrams-box{background:#fff;border:1px solid #e0d8cc;border-radius:8px;padding:1.25rem 1.5rem;margin-top:2.5rem}
.diagrams-box h3{margin-top:0;margin-bottom:0.75rem;font-size:1.1rem}
.diagram-grid{display:flex;flex-wrap:wrap;gap:0.5rem}
.diagram-grid a{background:#f0ebe3;border:1px solid #e0d8cc;border-radius:4px;padding:4px 10px;font-size:0.8rem;color:#2c2c2c;transition:background .15s}
.diagram-grid a:hover{background:#e0d8cc;text-decoration:none}
.footer{font-size:0.85rem;color:#888;margin-top:2.5rem;border-top:1px solid #e0d8cc;padding-top:1rem}"""


def _title_from_md(md_text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
    if m:
        return re.sub(r"[*_`]", "", m.group(1)).strip()
    return fallback.replace("_", " ").replace("-", " ").title()


def _nav_html(active_file: str) -> str:
    parts: list[str] = []
    for href, label in NAV_LINKS:
        cls = ' class="active"' if href == active_file else ""
        parts.append(f'<a href="{href}"{cls}>{label}</a>')
    return '<div class="header-nav">\n' + "\n".join(parts) + "\n</div>"


def _diagrams_html() -> str:
    links = "\n".join(
        f'<a href="{href}">{label}</a>' for label, href in DIAGRAM_LINKS
    )
    return (
        '<div class="diagrams-box">\n'
        '<h3>Architecture Diagrams</h3>\n'
        '<div class="diagram-grid">\n'
        f"{links}\n"
        "</div>\n"
        "</div>"
    )


def _rewrite_md_links(html_body: str) -> str:
    return re.sub(r'href="([^"]*?)\.md(#[^"]*?)?"', _md_link_replacer, html_body)


def _md_link_replacer(m: re.Match) -> str:
    path = m.group(1)
    fragment = m.group(2) or ""
    return f'href="{path}.html{fragment}"'


def convert(src: Path, dst: Path) -> None:
    md_text = src.read_text(encoding="utf-8")
    title = _title_from_md(md_text, src.stem)
    active_file = dst.name

    md_converter = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "smarty", "attr_list"],
        extension_configs={"toc": {"permalink": True, "permalink_title": "Link"}},
    )
    body = md_converter.convert(md_text)
    body = _rewrite_md_links(body)

    nav = _nav_html(active_file)
    diagrams = _diagrams_html()

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} &mdash; FinSight</title>
<style>
{STYLE}
</style>
</head>
<body>
<div class="container">
{nav}
{body}
{diagrams}
<div class="footer">FinSight &mdash; Multi-Agent Investment Research System</div>
</div>
</body>
</html>
"""
    dst.write_text(html, encoding="utf-8")
    print(f"  {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert project .md files to themed HTML")
    parser.add_argument(
        "files",
        nargs="*",
        help="Specific .md files to convert (default: all project docs)",
    )
    args = parser.parse_args()

    if args.files:
        pairs = []
        for f in args.files:
            src = Path(f).resolve()
            dst = DOCS / (src.stem + ".html")
            pairs.append((src, dst))
    else:
        pairs = SOURCES

    print(f"Converting {len(pairs)} file(s):")
    for src, dst in pairs:
        if not src.exists():
            print(f"  SKIP (not found): {src}")
            continue
        convert(src, dst)
    print("Done.")


if __name__ == "__main__":
    main()
