#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import random
import re
import smtplib
import ssl
import logging
import hashlib
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Tuple

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr

from playwright.async_api import async_playwright

# 🔐 local-only secrets
from secrets_local import QQ_SENDER_EMAIL, QQ_AUTH_CODE, RECIEVER_EMAIL


# ======================
# PATHS & LOGGING SETUP
# ======================
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / "visa_watcher.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("visa-watcher")


def log(tag: str, msg: str) -> None:
    logger.info(f"[{tag}] {msg}")


# ======================
# CONFIG
# ======================
URL = "https://toronto.rsvsys.jp/reservations/calendar"
PROFILE_DIR = Path("./chrome-profile")


# scan current + next N months
SCAN_NEXT_MONTHS = 3

# 🎯 jittered Gaussian delay configuration (means + std)
NO_AVAIL_MEAN_SECONDS = 90
NO_AVAIL_STD_SECONDS = 10

AFTER_EMAIL_MEAN_SECONDS = 600
AFTER_EMAIL_STD_SECONDS = 12

# crash restart backoff
CRASH_RESTART_MEAN_SECONDS = 45
CRASH_RESTART_STD_SECONDS = 10

# selectors
NEXT_SELECTOR = "a.next01.js_change_date"
MONTH_LABEL_SELECTOR = ".c_cal_navex_date .date"
CAL_TABLE_SELECTOR = "table.sc_cal_month"
CIRCLE_ICON_SUBSTR = "icon_circle.svg"

DOW_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ======================
# UTILS
# ======================
def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "").strip()


def jittered_gaussian_delay(mean: float, std: float) -> int:
    """
    Gaussian + jitter delay.
    Floor at 5 seconds to avoid negatives/extremes.
    """
    delay = random.gauss(mean, std) + random.uniform(-2, 2)
    return max(5, int(delay))


# ======================
# EMAIL (QQ SMTP SSL 465)
# ======================
def send_qq_email(sender_email: str, auth_code: str, receiver_email: str, subject: str, body: str) -> None:
    log("EMAIL", "Connecting to QQ SMTP server")
    msg = MIMEMultipart()
    msg["From"] = formataddr((str(Header("Visa Watcher", "utf-8")), sender_email))
    msg["To"] = receiver_email
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=context, timeout=30) as server:
        server.login(sender_email, auth_code)
        server.sendmail(sender_email, [receiver_email], msg.as_string())

    log("EMAIL", f"✅ Email sent to {receiver_email}")


# ======================
# CATEGORY ONLY SELECTION (ROBUST)
# ======================
async def _calendar_fingerprint(page) -> str:
    """
    Stable fingerprint (md5) of the calendar DOM.
    """
    html = await page.locator(CAL_TABLE_SELECTOR).first.inner_html()
    return hashlib.md5(html.encode("utf-8")).hexdigest()


async def _wait_calendar_changed_and_stable(page, old_fp_js: str, timeout_ms: int = 12000) -> None:
    """
    Wait until calendar content changes (cheap JS fingerprint differs),
    then becomes stable (md5 same twice).
    """
    # 1) wait until changed
    await page.wait_for_function(
        """([selector, oldFp]) => {
            const el = document.querySelector(selector);
            if (!el) return false;
            const html = el.innerHTML || "";
            const fp = `${html.length}:${html.slice(0,64)}:${html.slice(-64)}`;
            return fp !== oldFp;
        }""",
        arg=[CAL_TABLE_SELECTOR, old_fp_js],
        timeout=timeout_ms,
    )

    # 2) wait until stable
    await page.wait_for_timeout(300)
    fp1 = await _calendar_fingerprint(page)
    await page.wait_for_timeout(450)
    fp2 = await _calendar_fingerprint(page)
    if fp1 != fp2:
        await page.wait_for_timeout(650)


async def ensure_category_only(page) -> None:
    """
    Ensure only 'VISA Application' category is selected.
    Do NOT touch Application Details.

    Robust strategy:
    - capture calendar fingerprint before
    - select category (with multiple click strategies)
    - wait until calendar content changed + stabilized
    - retry up to 3 times
    - screenshot on failure
    """
    # Ensure calendar exists first
    await page.locator(CAL_TABLE_SELECTOR).first.wait_for(state="visible", timeout=12000)

    # Build cheap JS fingerprint string
    old_html = await page.locator(CAL_TABLE_SELECTOR).first.inner_html()
    old_fp_js = f"{len(old_html)}:{old_html[:64]}:{old_html[-64:] if len(old_html) >= 64 else old_html}"

    # If already selected, still ensure calendar is stable (not mid-refresh)
    try:
        await page.get_by_text("VISA Application", exact=False).first.wait_for(timeout=1200)
        log("SELECT", "Category already shows: VISA Application (ensuring calendar stable)")
        await page.wait_for_timeout(350)
        fp1 = await _calendar_fingerprint(page)
        await page.wait_for_timeout(450)
        fp2 = await _calendar_fingerprint(page)
        if fp1 != fp2:
            await page.wait_for_timeout(650)
        return
    except Exception:
        pass

    for attempt in range(1, 4):
        try:
            log("SELECT", f"Selecting category: VISA Application (attempt {attempt}/3)")

            # Find heading
            heading = page.get_by_text("Select a category", exact=False).first
            await heading.wait_for(state="visible", timeout=12000)

            # Try multiple open-picker strategies
            opened = False
            open_candidates = [
                heading.locator("xpath=following::a[1]"),
                heading.locator("xpath=following::*[@role='button'][1]"),
                heading.locator("xpath=following::button[1]"),
            ]

            for cand in open_candidates:
                try:
                    await cand.wait_for(state="visible", timeout=2500)
                    await cand.click(timeout=5000)
                    opened = True
                    break
                except Exception:
                    continue

            if not opened:
                # fallback: click near the heading block
                block = heading.locator("xpath=ancestor::*[self::div or self::section][1]")
                clickable = block.locator("a, [role='button'], button").first
                await clickable.click(timeout=5000)

            # Select option
            await page.get_by_text("VISA Application", exact=True).click(timeout=5000)

            # Wait calendar changed + stable
            await _wait_calendar_changed_and_stable(page, old_fp_js, timeout_ms=12000)

            log("SELECT", "Category selection done + calendar refreshed & stable")
            return

        except Exception as e:
            log("SELECT", f"Attempt {attempt} failed: {e}")

            # Debug screenshot
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = (LOG_DIR / f"select_fail_{ts}.png")
                await page.screenshot(path=str(screenshot_path), full_page=True)
                log("SELECT", f"Saved debug screenshot: {screenshot_path}")
            except Exception:
                pass

            await page.wait_for_timeout(900)

    raise RuntimeError("Failed to select VISA Application category after 3 attempts")


# ======================
# CALENDAR HELPERS
# ======================
async def get_year_month(page) -> Tuple[int, int]:
    el = page.locator(MONTH_LABEL_SELECTOR).first
    await el.wait_for(state="visible", timeout=8000)
    txt = normalize_ws(await el.inner_text())
    m = re.search(r"(\d{4})年(\d{1,2})月", txt)
    if not m:
        raise RuntimeError(f"Cannot parse year/month from header: {txt}")
    return int(m.group(1)), int(m.group(2))


async def get_month_label(page) -> str:
    y, m = await get_year_month(page)
    return f"{y}年{str(m).zfill(2)}月"


async def click_next_month(page) -> None:
    before = await get_month_label(page)
    log("NAV", f"Click next month (current: {before})")

    btn = page.locator(NEXT_SELECTOR).first
    await btn.wait_for(state="visible", timeout=8000)
    await btn.scroll_into_view_if_needed()

    try:
        await btn.click(timeout=5000)
    except Exception:
        await btn.click(timeout=5000, force=True)

    await page.wait_for_timeout(700)
    after = await get_month_label(page)
    if after == before:
        log("NAV", "Month label unchanged, trying JS click fallback")
        await page.evaluate(
            "(selector) => document.querySelector(selector)?.click()",
            NEXT_SELECTOR,
        )
        await page.wait_for_timeout(700)
        after = await get_month_label(page)

    log("NAV", f"Moved to {after}")


async def available_dates_current_month(page) -> List[Tuple[str, str]]:
    year, month = await get_year_month(page)
    log("DETECT", f"Scanning {year}-{month:02d}")

    results: List[Tuple[str, str]] = []

    imgs = page.locator(
        f"{CAL_TABLE_SELECTOR} tbody.sc_cal_month_tbody img[src*='{CIRCLE_ICON_SUBSTR}']"
    )
    count = await imgs.count()
    log("DETECT", f"Found {count} circle marker(s) in DOM")

    for i in range(count):
        td = imgs.nth(i).locator("xpath=ancestor::td[1]")
        text = (await td.inner_text()).strip()
        m = re.search(r"\b(\d{1,2})\b", text)
        if not m:
            continue

        try:
            d = date(year, month, int(m.group(1)))
        except ValueError:
            continue

        results.append((d.isoformat(), DOW_EN[d.weekday()]))

    results = sorted(set(results))
    if results:
        for d, dow in results:
            log("DETECT", f"Available: {d} ({dow})")
    else:
        log("DETECT", "No available dates in this month")

    return results


async def scan_months(page) -> Dict[str, List[Tuple[str, str]]]:
    """
    Scan current month + next SCAN_NEXT_MONTHS months.
    We do not navigate back; a page reload resets to default.
    """
    found: Dict[str, List[Tuple[str, str]]] = {}

    for i in range(SCAN_NEXT_MONTHS + 1):
        label = await get_month_label(page)
        log("SCAN", f"Month {i+1}/{SCAN_NEXT_MONTHS+1}: {label}")
        found[label] = await available_dates_current_month(page)

        if i < SCAN_NEXT_MONTHS:
            await click_next_month(page)

    return found


def format_email(results: Dict[str, List[Tuple[str, str]]]) -> str:
    lines = ["Available visa appointment dates:\n"]
    total = 0
    for month, items in results.items():
        if not items:
            continue
        lines.append(f"{month}:")
        for d, dow in items:
            lines.append(f"  - {d} ({dow})")
            total += 1
        lines.append("")
    if total == 0:
        return "No available dates."
    lines.append(f"Total: {total}")
    lines.append(f"Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Page: {URL}")
    return "\n".join(lines)


async def refresh_and_reselect(page) -> None:
    log("REFRESH", "Reloading page")
    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)
    await page.locator(CAL_TABLE_SELECTOR).first.wait_for(state="visible", timeout=12000)
    await ensure_category_only(page)


# ======================
# ONE BROWSER SESSION (will raise if Chrome crashes)
# ======================
async def run_browser_session() -> None:
    async with async_playwright() as p:
        log("SUPERVISOR", "Launching Chrome persistent context")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            viewport={"width": 1400, "height": 900},
        )

        try:
            page = await context.new_page()

            log("INIT", f"Opening {URL}")
            await page.goto(URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1400)

            log("SELECT", "Selecting category at start (category only)")
            await ensure_category_only(page)

            last_signature = None

            while True:
                results = await scan_months(page)
                has_avail = any(results[m] for m in results)

                if has_avail:
                    sig = tuple((m, tuple(v)) for m, v in results.items())
                    if sig != last_signature:
                        log("EMAIL", "New availability detected — sending email")
                        send_qq_email(
                            QQ_SENDER_EMAIL,
                            QQ_AUTH_CODE,
                            RECEIVER_EMAIL,
                            "Japan Visa Appointment Availability (Toronto)",
                            format_email(results),
                        )
                        last_signature = sig
                    else:
                        log("EMAIL", "Availability unchanged — not sending again")

                    delay = jittered_gaussian_delay(AFTER_EMAIL_MEAN_SECONDS, AFTER_EMAIL_STD_SECONDS)
                    log("SLEEP", f"Availability detected — sleeping {delay}s (gaussian)")

                else:
                    delay = jittered_gaussian_delay(NO_AVAIL_MEAN_SECONDS, NO_AVAIL_STD_SECONDS)
                    log("SLEEP", f"No availability — sleeping {delay}s (gaussian)")

                await asyncio.sleep(delay)
                await refresh_and_reselect(page)

        finally:
            try:
                await context.close()
            except Exception:
                pass


# ======================
# SUPERVISOR: AUTO-RESTART ON CRASH
# ======================
async def main():
    PROFILE_DIR.mkdir(exist_ok=True)
    log("INIT", f"Visa watcher supervisor started. Log file: {LOG_FILE}")

    while True:
        try:
            await run_browser_session()
        except Exception as e:
            log("CRASH", f"Browser crashed/disconnected: {e}")
            delay = jittered_gaussian_delay(CRASH_RESTART_MEAN_SECONDS, CRASH_RESTART_STD_SECONDS)
            log("SUPERVISOR", f"Restarting browser in {delay}s")
            await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
