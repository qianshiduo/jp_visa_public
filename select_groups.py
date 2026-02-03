# select_groups.py
import asyncio
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


async def _click_text_option(page, text: str, timeout_ms: int = 8000) -> None:
    """
    Click an option by its visible text, preferring exact match but falling back to partial.
    Works for modals/lists, not just <select>.
    """
    # Try exact text first
    exact = page.get_by_text(text, exact=True)
    try:
        await exact.first.wait_for(state="visible", timeout=timeout_ms)
        await exact.first.click(timeout=timeout_ms)
        return
    except Exception:
        pass

    # Fallback: partial match
    partial = page.get_by_text(text, exact=False)
    await partial.first.wait_for(state="visible", timeout=timeout_ms)
    await partial.first.click(timeout=timeout_ms)


async def _ensure_text_visible_in_page(page, text: str, timeout_ms: int = 8000) -> None:
    """
    Wait until the selected text is visible somewhere on the page.
    """
    await page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=timeout_ms)


async def _open_picker_by_heading(page, heading_text: str, timeout_ms: int = 8000) -> None:
    """
    Open the picker field that belongs to a heading like:
      'Select a category'
      'Select Application Details'

    Strategy: find the heading text, then click the first "field-like" element after it:
    - a large clickable row often contains an arrow icon and the current selected value.
    We click the nearest container.
    """
    heading = page.get_by_text(heading_text, exact=False).first
    await heading.wait_for(state="visible", timeout=timeout_ms)

    # Try to click the next clickable row after the heading:
    # Many UIs use a big <a> or <div role="button"> under the heading.
    # We'll try common patterns in descending reliability.
    candidates = [
        # clickable anchors under the same section
        heading.locator("xpath=following::a[1]"),
        # role button
        heading.locator("xpath=following::*[@role='button'][1]"),
        # fallback: next div (often the big selection row)
        heading.locator("xpath=following::div[1]"),
    ]

    last_err = None
    for c in candidates:
        try:
            await c.wait_for(state="visible", timeout=timeout_ms)
            await c.click(timeout=timeout_ms)
            return
        except Exception as e:
            last_err = e

    raise RuntimeError(f"Could not open picker for heading '{heading_text}': {last_err}")


async def ensure_visa_group_selected(
    page,
    *,
    category_text: str = "VISA Application",
    details_text: str = "VISA Application up to 4 applicants",
    timeout_ms: int = 8000,
) -> None:
    """
    Ensure the correct group is selected:
      - Category: 'VISA Application'
      - Details: 'VISA Application up to 4 applicants ...'

    Call this after page load and after each refresh/reload.
    """
    # 1) Category
    try:
        # If already selected, no-op
        await page.get_by_text(category_text, exact=False).first.wait_for(state="visible", timeout=1500)
    except PlaywrightTimeoutError:
        # Open category picker and select
        await _open_picker_by_heading(page, "Select a category", timeout_ms=timeout_ms)
        await _click_text_option(page, category_text, timeout_ms=timeout_ms)
        await _ensure_text_visible_in_page(page, category_text, timeout_ms=timeout_ms)

    # Tiny settle time for UI
    await page.wait_for_timeout(300)

    # 2) Application details
    # The details line on the site includes bilingual text, so we match the English prefix.
    try:
        await page.get_by_text(details_text, exact=False).first.wait_for(state="visible", timeout=1500)
    except PlaywrightTimeoutError:
        await _open_picker_by_heading(page, "Select Application Details", timeout_ms=timeout_ms)
        await _click_text_option(page, details_text, timeout_ms=timeout_ms)
        await _ensure_text_visible_in_page(page, details_text, timeout_ms=timeout_ms)

    # Final settle
    await page.wait_for_timeout(300)
