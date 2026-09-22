#!/usr/bin/env python3
"""
Earlier — Bupa Medical Visa Services appointment slot watcher
=============================================================

Watches the official OASIS booking portal for an *earlier* visa health
examination slot and sends a Telegram alert when one appears.

Portal (do not guess this URL — it is the live booking system):
    https://bmvs.onlineappointmentscheduling.net.au/oasis/

This script:
  * launches Chromium with stealth flags (Playwright)
  * walks the public booking wizard OR intercepts AppointmentTime.aspx
  * parses `var gAvailDates = [...]` (the calendar's JS payload)
  * notifies Telegram only when the earliest date is NEW and earlier
    than both your target and the last date you were already told about
  * never books, pays, or submits HAP ID / passport data

Polling is randomised (default 180–300 s) so you are not hammering the
portal. Personal use only. Respect Bupa's terms.

Quick start
-----------
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium
    cp env.example.txt .env   # then edit
    python bupa_slot_watch.py

How to find / swap selectors
----------------------------
OASIS is ASP.NET WebForms (NEXA OASIS). IDs are stable-ish but the
wizard pages *do* change. After you run headed once (`HEADLESS=0`):

  1. Open DevTools → Elements, search for the control, copy its `id`.
  2. Replace the matching entry in SELECTORS below.
  3. Open DevTools → Network, tick "Preserve log", walk the wizard.
     Filter: `AppointmentTime`  or  `aspx`.
  4. Click a response → Response tab → search `gAvailDates`.
     Example:
         var gAvailDates = [new Date(2026, 9, 12), new Date(2026, 10, 3)];
     JavaScript months are 0-based: 9 = October, 10 = November.
  5. If you see a JSON XHR instead, drop its URL into JSON_HINT_URLS
     and the intercept path will parse it automatically.

Telegram
--------
    1. Telegram → @BotFather → /newbot → copy the token.
    2. Open a chat with your bot, send /start.
    3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates
       and read `message.chat.id` (for a group it is negative).
    4. Put both values in the environment (see env.example.txt).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

# ---------------------------------------------------------------------------
# Configuration — override with environment variables (see env.example.txt)
# ---------------------------------------------------------------------------

BOOKING_HOME = "https://bmvs.onlineappointmentscheduling.net.au/oasis/"
APPOINTMENT_TIME_URL = (
    "https://bmvs.onlineappointmentscheduling.net.au/oasis/AppointmentTime.aspx"
)

# City / clinic text as it appears in OASIS (partial match is enough).
LOCATION_QUERY = os.environ.get("LOCATION_QUERY", "Darwin")
CLINIC_NAME = os.environ.get("CLINIC_NAME", "Jobfit Darwin")
LOCATION_STATE = os.environ.get("LOCATION_STATE", "NT")

# Alert when the earliest available slot is *strictly before* this date.
TARGET_DATE = date.fromisoformat(os.environ.get("TARGET_DATE", "2026-11-01"))

# "intercept" = walk wizard once, then reload AppointmentTime.aspx and
# parse gAvailDates / JSON.  "wizard" = re-click through location/exam
# each loop (slower, use if session drops).
STRATEGY = os.environ.get("STRATEGY", "wizard").strip().lower()

POLL_MIN_SECONDS = int(os.environ.get("POLL_MIN_SECONDS", "180"))
POLL_MAX_SECONDS = int(os.environ.get("POLL_MAX_SECONDS", "300"))
HEADLESS = os.environ.get("HEADLESS", "1") not in {"0", "false", "False"}
TIMEZONE = os.environ.get("TZ_NAME", "Australia/Darwin")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

RUN_ONCE = os.environ.get("RUN_ONCE", "0").lower() in {"1", "true", "yes"} or "--once" in sys.argv

STATE_PATH = Path(os.environ.get("STATE_PATH", "./state.json"))
SCREENSHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "./screenshots"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "45000"))

# URLs whose JSON bodies we will try to parse for dates. Add any XHR you
# discover in DevTools (Network → Fetch/XHR) here.
JSON_HINT_URLS = (
    "AppointmentTime",
    "GetAvailability",
    "available",
    "Calendar",
    "SelectTime",
)

# ---------------------------------------------------------------------------
# SELECTORS — confirmed against OASIS v23 (NEXA) plus placeholders to swap
# ---------------------------------------------------------------------------
#
# Confirmed on the landing page / modify-booking flow:
#   #ContentPlaceHolder1_btnInd          New Individual booking
#   #ContentPlaceHolder1_btnFam          New Family booking
#   #ContentPlaceHolder1_btnCont         Continue / Next
#   #ContentPlaceHolder1_SelectTime1_*   Calendar + time radios
#   div.am-list / div.pm-list            Time labels on AppointmentTime.aspx
#
# Location + exam-type pages vary by wizard revision. The values below are
# *intentionally broad* (id contains / placeholder contains). When they
# miss, run headed, copy the real id from Elements, and replace the string.

SELECTORS = {
    # Landing
    "new_individual": "#ContentPlaceHolder1_btnInd",
    "new_family": "#ContentPlaceHolder1_btnFam",
    "continue": "#ContentPlaceHolder1_btnCont, button:has-text('Continue'), button:has-text('Next')",
    # Location (REPLACE after inspecting the live page)
    "location_search": "#ContentPlaceHolder1_SelectLocation1_txtSuburb",
    "location_state": "#ContentPlaceHolder1_SelectLocation1_ddlState",
    "location_search_btn": "input.blue-button[value='Search']",
    "location_result": "tr.trlocation, input.rbLocation",
    "exam_checkboxes": "input[id^='chkClass1_']",
    # Calendar / times (confirmed)
    "calendar_root": (
        "#divPaginationNavigation, #ContentPlaceHolder1_SelectTime1_txtAppDate, "
        ".ui-datepicker-calendar"
    ),
    "available_day": (
        "#divPaginationNavigation a, a.available, "
        ".ui-datepicker-calendar td:not(.ui-datepicker-unselectable) a"
    ),
    "am_slots": "div.am-list label",
    "pm_slots": "div.pm-list label",
    "time_radios": (
        "#ContentPlaceHolder1_SelectTime1_rblResults input, "
        "#ContentPlaceHolder1_SelectTime1_divSearchResults input"
    ),
}


# ---------------------------------------------------------------------------
# Logging / state
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("earlier")


@dataclass
class SlotHit:
    day: date
    time: str | None
    raw_dates: list[str]

    @property
    def iso(self) -> str:
        return self.day.isoformat()


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("state file was corrupt — starting fresh")
    return {"last_alerted_date": None, "last_check_iso": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Date parsing — the reliable OASIS signal is `gAvailDates`
# ---------------------------------------------------------------------------

_JS_DATE = re.compile(
    r"new Date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})",
)
_ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_GAVAIL = re.compile(r"var\s+gAvailDates\s*=\s*\[(.*?)\];", re.S)


def _valid_slot_day(day: date) -> bool:
    """Drop epoch junk and dates too far out (jQuery UI has new Date(1970, 1, 1))."""
    today = date.today()
    try:
        horizon = today.replace(year=today.year + 2)
    except ValueError:
        horizon = date(today.year + 2, 3, 1)
    return today <= day <= horizon


def parse_gavail_dates(html: str) -> list[date]:
    """Extract dates from `var gAvailDates = [new Date(y, m, d), ...];`.

    JS months are 0-based. Empty `gAvailDates = []` means no slots — do not
    fall back to scanning the whole page (that picks up library epoch dates).
    """
    found: list[date] = []
    block = _GAVAIL.search(html)
    if not block:
        return []
    haystack = block.group(1)
    if not haystack.strip():
        return []
    for year, month0, day in _JS_DATE.findall(haystack):
        try:
            found.append(date(int(year), int(month0) + 1, int(day)))
        except ValueError:
            continue
    return sorted({d for d in found if _valid_slot_day(d)})


def parse_dates_from_json(payload: Any) -> list[date]:
    """Best-effort walk of a JSON body looking for date-like values."""
    found: list[date] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            for iso in _ISO_DATE.findall(node):
                try:
                    found.append(date.fromisoformat(iso))
                except ValueError:
                    pass
            for year, month0, day in _JS_DATE.findall(node):
                try:
                    found.append(date(int(year), int(month0) + 1, int(day)))
                except ValueError:
                    pass

    walk(payload)
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(html: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing alert instead:\n%s", html)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", r.text))
        log.info("Telegram alert sent")
    except Exception as exc:  # noqa: BLE001 — never crash the loop
        log.error("Telegram failed: %s", exc)


def compose_alert(hit: SlotHit) -> str:
    time_bit = f" at {hit.time}" if hit.time else ""
    return (
        "<b>Earlier slot found</b>\n\n"
        f"Location: {LOCATION_QUERY} — {CLINIC_NAME}\n"
        f"Earliest: <b>{hit.day.strftime('%d %B %Y').lstrip('0')}</b>{time_bit}\n"
        f"Your target: before {TARGET_DATE.strftime('%d %B %Y').lstrip('0')}\n\n"
        f'<a href="{BOOKING_HOME}">Open the Bupa OASIS booking portal</a>\n\n'
        "<i>This bot does not book for you. Open the link and complete the booking yourself.</i>"
    )


# ---------------------------------------------------------------------------
# Playwright stealth
# ---------------------------------------------------------------------------

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-features=IsolateOrigins,site-per-process",
]

STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-AU', 'en'] });
Object.defineProperty(navigator, 'language', { get: () => 'en-AU' });
window.chrome = { runtime: {} };
"""


async def new_context(browser: Browser) -> BrowserContext:
    context = await browser.new_context(
        locale="en-AU",
        timezone_id=TIMEZONE,
        viewport={"width": 1366, "height": 860},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
    )
    await context.add_init_script(STEALTH_INIT)
    return context


# ---------------------------------------------------------------------------
# Wizard + intercept
# ---------------------------------------------------------------------------

class Capture:
    """Collects dates spotted in network bodies during a session."""

    def __init__(self) -> None:
        self.dates: list[date] = []
        self.json_urls: list[str] = []

    async def on_response(self, response: Response) -> None:
        url = response.url
        ctype = (response.headers or {}).get("content-type", "")
        try:
            body = await response.text()
        except Exception:
            return
        if "gAvailDates" in body and "AppointmentTime" in url:
            parsed = parse_gavail_dates(body)
            if parsed:
                log.info("Intercepted gAvailDates from %s → %s", url, parsed[:6])
                self.dates = sorted(set(self.dates + parsed))
        if "json" in ctype.lower() or any(h in url for h in JSON_HINT_URLS):
            if url not in self.json_urls:
                self.json_urls.append(url)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return
            parsed = parse_dates_from_json(payload)
            if parsed:
                parsed = [d for d in parsed if _valid_slot_day(d)]
            if parsed:
                log.info("Intercepted JSON dates from %s → %s", url, parsed[:6])
                self.dates = sorted(set(self.dates + parsed))


async def click_first(page: Page, selector: str, timeout: int = 8000) -> bool:
    loc = page.locator(selector).first
    try:
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        return True
    except PlaywrightTimeout:
        return False


async def fill_first(page: Page, selector: str, value: str, timeout: int = 8000) -> bool:
    loc = page.locator(selector).first
    try:
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.fill(value)
        return True
    except PlaywrightTimeout:
        return False


async def walk_wizard(page: Page) -> None:
    """Public booking path: landing → individual → location → exam → calendar.

    Stops once AppointmentTime.aspx (or gAvailDates) is in play. Never fills
    HAP ID, passport, or payment.
    """
    log.info("Navigating to OASIS landing")
    await page.goto(BOOKING_HOME, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_timeout(800)

    if not await click_first(page, SELECTORS["new_individual"], timeout=12000):
        # Text fallback if the id moved
        await page.get_by_role("button", name=re.compile("individual", re.I)).click()
    log.info("Clicked New Individual booking")
    await page.wait_for_timeout(800)

    suburb = page.locator(SELECTORS["location_search"]).first
    try:
        await suburb.wait_for(state="visible", timeout=15000)
        await suburb.fill(LOCATION_QUERY)
        log.info("Typed suburb %r", LOCATION_QUERY)
    except PlaywrightTimeout:
        log.warning("Suburb box not found")

    if LOCATION_STATE:
        try:
            await page.select_option(SELECTORS["location_state"], LOCATION_STATE)
            log.info("Selected state %s", LOCATION_STATE)
        except Exception as exc:
            log.warning("State dropdown: %s", exc)

    if not await click_first(page, SELECTORS["location_search_btn"], timeout=8000):
        await page.keyboard.press("Enter")
    log.info("Clicked Search")
    await page.wait_for_timeout(2500)

    picked = False
    for needle in (CLINIC_NAME, LOCATION_QUERY):
        loc = page.get_by_text(needle, exact=False).first
        try:
            await loc.wait_for(state="visible", timeout=8000)
            await loc.click()
            picked = True
            log.info("Selected location matching %r", needle)
            break
        except PlaywrightTimeout:
            continue
    if not picked:
        if await click_first(page, SELECTORS["location_result"], timeout=4000):
            log.info("Selected first location result")
        else:
            log.warning("Could not click a location row")

    if await click_first(page, SELECTORS["continue"], timeout=8000):
        log.info("Continue after location")
        await page.wait_for_timeout(800)

    exam = page.get_by_text("Medical Examination (501)", exact=False).first
    try:
        await exam.wait_for(state="visible", timeout=10000)
        await exam.click()
        log.info("Ticked Medical Examination (501)")
    except PlaywrightTimeout:
        boxes = page.locator(SELECTORS["exam_checkboxes"])
        if await boxes.count():
            await boxes.first.click()
            log.info("Ticked first exam checkbox")

    if await click_first(page, SELECTORS["continue"], timeout=8000):
        log.info("Continue after exam type")

    # Wait until the calendar payload is on the page (or we time out).
    try:
        await page.wait_for_function(
            "() => document.body && document.body.innerHTML.includes('gAvailDates')",
            timeout=NAV_TIMEOUT_MS,
        )
        log.info("gAvailDates appeared in the document")
    except PlaywrightTimeout:
        log.warning(
            "gAvailDates not in DOM yet — will still scrape AppointmentTime.aspx / network"
        )


async def extract_times(page: Page) -> str | None:
    for sel in (SELECTORS["am_slots"], SELECTORS["pm_slots"], SELECTORS["time_radios"]):
        loc = page.locator(sel)
        try:
            if await loc.count():
                text = (await loc.first.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return None


async def collect_hit(page: Page, capture: Capture) -> SlotHit | None:
    html = await page.content()
    dates = parse_gavail_dates(html)
    if capture.dates:
        dates = sorted({d for d in dates + capture.dates if _valid_slot_day(d)})
    if not dates:
        # Last-ditch: evaluate the JS global if the page defined it.
        try:
            raw = await page.evaluate(
                """() => {
                    if (typeof gAvailDates === 'undefined' || !gAvailDates || !gAvailDates.length) return [];
                    const out = [];
                    for (const pageDates of gAvailDates) {
                      const items = Array.isArray(pageDates) ? pageDates : [pageDates];
                      for (const d of items) {
                        if (!(d instanceof Date) || isNaN(d.getTime())) continue;
                        const y = d.getFullYear();
                        const m = String(d.getMonth() + 1).padStart(2, '0');
                        const day = String(d.getDate()).padStart(2, '0');
                        out.push(`${y}-${m}-${day}`);
                      }
                    }
                    return out;
                }"""
            )
            dates = [
                date.fromisoformat(x)
                for x in raw
                if isinstance(x, str) and _valid_slot_day(date.fromisoformat(x))
            ]
        except Exception:
            dates = []
    if not dates:
        return None
    earliest = dates[0]
    time_label = await extract_times(page)
    return SlotHit(
        day=earliest,
        time=time_label,
        raw_dates=[d.isoformat() for d in dates],
    )


async def snapshot(page: Page, tag: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot %s", path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Screenshot failed: %s", exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def should_alert(hit: SlotHit, last_alerted: str | None) -> bool:
    if not _valid_slot_day(hit.day):
        return False
    if hit.day >= TARGET_DATE:
        return False
    if last_alerted:
        try:
            prev = date.fromisoformat(last_alerted)
            # Epoch / leftover junk must not block a real future slot.
            if prev >= date.today() and hit.iso >= last_alerted:
                return False
        except ValueError:
            pass
    return True


async def one_check(page: Page, capture: Capture, first: bool) -> SlotHit | None:
    if first or STRATEGY == "wizard":
        await walk_wizard(page)
    else:
        # Keep the WebForms session: reload the calendar page.
        try:
            if "AppointmentTime" not in page.url:
                await page.goto(
                    APPOINTMENT_TIME_URL,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                )
            else:
                await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            log.warning("Reload timed out (%s) — re-walking wizard", exc)
            await walk_wizard(page)
    await page.wait_for_timeout(600)
    return await collect_hit(page, capture)


async def run() -> None:
    if POLL_MIN_SECONDS < 60:
        log.warning("POLL_MIN_SECONDS is aggressive — raising to 60 to avoid rate limits")
    poll_min = max(60, POLL_MIN_SECONDS)
    poll_max = max(poll_min, POLL_MAX_SECONDS)

    state = load_state()
    log.info(
        "Watching %s / %s  |  target before %s  |  strategy=%s  |  poll %s–%ss",
        LOCATION_QUERY,
        CLINIC_NAME,
        TARGET_DATE.isoformat(),
        STRATEGY,
        poll_min,
        poll_max,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, args=STEALTH_ARGS)
        context = await new_context(browser)
        page = await context.new_page()
        capture = Capture()
        page.on("response", capture.on_response)

        first = True
        failures = 0
        try:
            while True:
                try:
                    hit = await one_check(page, capture, first=first)
                    first = False
                    failures = 0
                    state["last_check_iso"] = datetime.now(timezone.utc).isoformat()
                    if not hit:
                        log.info("No dates parsed this round")
                        await snapshot(page, "empty")
                    else:
                        log.info(
                            "Earliest %s%s  (n=%s)  last_alerted=%s",
                            hit.iso,
                            f" {hit.time}" if hit.time else "",
                            len(hit.raw_dates),
                            state.get("last_alerted_date"),
                        )
                        if should_alert(hit, state.get("last_alerted_date")):
                            send_telegram(compose_alert(hit))
                            state["last_alerted_date"] = hit.iso
                            log.info("Alert recorded for %s", hit.iso)
                        else:
                            log.info("No Telegram — not earlier than target / last alert")
                    save_state(state)
                except PlaywrightTimeout as exc:
                    failures += 1
                    log.error("Timeout: %s", exc)
                    await snapshot(page, "timeout")
                except Exception as exc:  # noqa: BLE001 — keep the loop alive
                    failures += 1
                    log.error("Check failed: %s\n%s", exc, traceback.format_exc())
                    await snapshot(page, "error")
                    if failures >= 3:
                        log.warning("Recycling browser after %s failures", failures)
                        try:
                            await context.close()
                        except Exception:
                            pass
                        context = await new_context(browser)
                        page = await context.new_page()
                        capture = Capture()
                        page.on("response", capture.on_response)
                        first = True
                        failures = 0

                if RUN_ONCE:
                    log.info("RUN_ONCE set — exiting after this check")
                    break

                delay = random.randint(poll_min, poll_max)
                log.info("Sleeping %ss", delay)
                await asyncio.sleep(delay)
        finally:
            await context.close()
            await browser.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
