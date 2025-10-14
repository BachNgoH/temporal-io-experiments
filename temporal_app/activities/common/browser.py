from contextlib import asynccontextmanager
from typing import AsyncIterator, Tuple, Any

from playwright.async_api import async_playwright


@asynccontextmanager
async def launch_chromium_page(*, headless: bool = True, context_kwargs: dict[str, Any] | None = None) -> AsyncIterator[Tuple[Any, Any, Any]]:
    """Launch a Chromium browser, create a context and page, and yield them.

    Usage:
        async with launch_chromium_page() as (browser, context, page):
            await page.goto("https://example.com")
    """
    context_kwargs = context_kwargs or {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        try:
            yield browser, context, page
        finally:
            await context.close()
            await browser.close()


