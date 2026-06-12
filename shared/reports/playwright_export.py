# ruff: noqa: E402
"""Playwright-based HTML → PPTX/PDF export using deck-stage's built-in APIs."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO

logger = logging.getLogger(__name__)


async def html_to_pptx(html: str) -> BytesIO:
    """Convert HTML deck to PPTX via headless Chromium screenshots."""
    from playwright.async_api import async_playwright

    html = html.replace("<deck-stage ", "<deck-stage noscale ")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.set_content(html, wait_until="networkidle")
        # Wait for custom element to define, upgrade, and populate slides
        await page.wait_for_function(
            """() => {
                const ds = document.querySelector('deck-stage');
                if (!ds || !customElements.get('deck-stage')) return false;
                if (ds.hasAttribute('data-fonts-pending')) return false;
                return ds._slides && ds._slides.length > 0;
            }""",
            timeout=15000,
        )
        total = await page.evaluate("document.querySelector('deck-stage')._slides.length")

        from pptx import Presentation
        from pptx.util import Emu

        prs = Presentation()
        prs.slide_width = Emu(12192000)
        prs.slide_height = Emu(6858000)
        blank_layout = prs.slide_layouts[6]

        for i in range(total):
            await page.evaluate(f"document.querySelector('deck-stage').goTo({i})")
            await page.wait_for_timeout(150)
            section = await page.query_selector("section[data-deck-active]")
            if section:
                screenshot = await section.screenshot(type="png")
                slide = prs.slides.add_slide(blank_layout)
                slide.shapes.add_picture(
                    BytesIO(screenshot), Emu(0), Emu(0), Emu(12192000), Emu(6858000)
                )

        await browser.close()

        buf = BytesIO()
        prs.save(buf)
        buf.seek(0)
        return buf


async def html_to_pdf(html: str) -> BytesIO:
    """Convert HTML deck to PDF via headless Chromium print mode."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.set_content(html, wait_until="networkidle")
        await page.emulate_media(media="print")
        buf = BytesIO(
            await page.pdf(landscape=True, width="1920px", height="1080px", print_background=True)
        )
        await browser.close()
        buf.seek(0)
        return buf


def html_to_pptx_sync(html: str) -> BytesIO:
    return asyncio.run(html_to_pptx(html))


def html_to_pdf_sync(html: str) -> BytesIO:
    return asyncio.run(html_to_pdf(html))
