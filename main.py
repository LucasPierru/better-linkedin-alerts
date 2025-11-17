# linkedin_playwright_description_filter.py
# Requires: playwright, pandas
# pip install playwright pandas
# python -m playwright install

import time
import csv
from urllib.parse import quote_plus
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# -----------------------------
# CONFIG - edit before running
# -----------------------------
# Path to a directory that Playwright will use as a persistent profile.
# IMPORTANT: This folder must be the directory that contains your Chrome/Chromium user profile data,
# or just point to an empty folder to create a new persistent profile and sign in manually once.
#
# Example values:
# Windows: r"C:\Users\YOU\AppData\Local\Google\Chrome\User Data\Default"
# macOS: "/Users/you/Library/Application Support/Google/Chrome/Default"
# Linux: "/home/you/.config/google-chrome/Default"
PERSISTENT_PROFILE_DIR = r"C:/Users/lucas/AppData/Local/Google/Chrome/User Data/Default"  # <- change this

# If you want Playwright to use the installed Chrome (instead of bundled Chromium), set channel to "chrome".
# Otherwise set to None to use default chromium.
BROWSER_CHANNEL = "chrome"  # or None

KEYWORDS_REQUIRED = ["developer", "next.js", "react.js", "javascript", "typescript"]       # keywords to look for *in the description*
MATCH_MODE = "any"                   # "any" or "all"
EXCLUDE_KEYWORDS = ["senior", "lead", "manager", "5+ years"]
CASE_INSENSITIVE = True

OUTPUT_CSV = "linkedin_playwright_filtered_jobs.csv"
WAIT_AFTER_NAV = 2.5          # seconds to wait after navigation
SCROLL_PAUSE = 0.8
MAX_MATCHES_PER_SEARCH = None  # None to collect all matches, or int to limit
# -----------------------------

SEARCHES = [
    {"location": "Montreal, QC, Canada", "work_type": "onsite"},
    {"location": "Montreal, QC, Canada", "work_type": "hybrid"},
    {"location": "Montreal, QC, Canada", "work_type": "remote"},
    {"location": "Canada", "work_type": "remote"},
]

WORK_TYPE_MAP = {"onsite": "1", "remote": "2", "hybrid": "3"}


def build_linkedin_url(keywords, location, work_type=None, posted_seconds=86400):
    base = "https://www.linkedin.com/jobs/search/"
    q = f"?keywords={quote_plus(keywords)}&location={quote_plus(location)}"
    q += f"&f_TPR=r{posted_seconds}&sortBy=DD"
    if work_type:
        wt = WORK_TYPE_MAP.get(work_type.lower())
        if wt:
            q += f"&f_WT={wt}"
    return base + q


def normalize_text(s):
    if s is None:
        return ""
    return s.lower() if CASE_INSENSITIVE else s


def description_matches(description, keywords, mode="any", exclude=None):
    desc = normalize_text(description)
    keys = [normalize_text(k) for k in (keywords or [])]
    exc = [normalize_text(e) for e in (exclude or [])]

    # exclude check
    for e in exc:
        if e and e in desc:
            return False

    if mode == "all":
        return all(k in desc for k in keys)
    else:
        return any(k in desc for k in keys)


def scroll_results_list(page, container_selector="div.scaffold-layout__list, div.jobs-search-results-list"):
    # Try scrolling the results container; fallback to window scroll
    try:
        container = page.query_selector(container_selector)
        if container:
            prev = -1
            start = time.time()
            while True:
                page.evaluate("(el) => el.scrollTo(0, el.scrollHeight)", container)
                time.sleep(SCROLL_PAUSE)
                # compute new height
                height = page.evaluate("(el) => el.scrollHeight", container)
                if height == prev:
                    break
                prev = height
                if time.time() - start > 10:
                    break
            return
    except Exception:
        pass
    # fallback whole page
    prev = -1
    start = time.time()
    while True:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_PAUSE)
        height = page.evaluate("() => document.body.scrollHeight")
        if height == prev:
            break
        prev = height
        if time.time() - start > 10:
            break


# Possible selectors for the description panel — keep list to adapt if LinkedIn changes DOM.
DESCRIPTION_SELECTORS = [
    "div.jobs-description__container",
    "div.jobs-description-content__text",
    "section.show-more-less-html",
    "div.jobs-box__html-content",
    "div.description__text",
]


def extract_description_from_right_panel(page, timeout=2000):
    # Try each selector, return first non-empty text
    for sel in DESCRIPTION_SELECTORS:
        try:
            element = page.query_selector(sel)
            if element:
                text = element.inner_text(timeout=timeout).strip()
                if text:
                    return text
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    # Fallback: grab big chunk of page text (not ideal)
    try:
        body = page.inner_text("body")
        return body[:10000]
    except Exception:
        return ""


def extract_metadata_after_click(page, job_href):
    # title, company, location, posted
    title = ""
    company = ""
    location = ""
    posted = ""
    try:
        # Many LinkedIn jobs render title in h1 or h2 with class including 'topcard'
        t = page.query_selector("h1.topcard__title, h2.topcard__title, h1[class*='job-title']")
        if t:
            title = t.inner_text().strip()
    except Exception:
        pass
    try:
        c = page.query_selector("a.topcard__org-name-link, span.topcard__org-name, a.topcard__flavor--black-link")
        if c:
            company = c.inner_text().strip()
    except Exception:
        pass
    try:
        loc = page.query_selector("span.topcard__flavor--bullet, span.topcard__flavor, .jobs-unified-top-card__bullet")
        if loc:
            location = loc.inner_text().strip()
    except Exception:
        pass
    try:
        time_el = page.query_selector("time, span.posted-time-ago__text")
        if time_el:
            posted = time_el.inner_text().strip()
    except Exception:
        pass

    return {"title": title, "company": company, "location": location, "posted": posted, "link": job_href}


def fetch_matches_for_search(page, search, keywords_required, match_mode, exclude_keywords, max_matches=None):
    location = search.get("location")
    work_type = search.get("work_type")
    url = build_linkedin_url(" ", location, work_type=work_type, posted_seconds=86400)
    print("Opening:", url)
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(WAIT_AFTER_NAV)

    # scroll results pane
    scroll_results_list(page)

    # collect job anchors referencing /jobs/view/
    job_locators = page.locator('a[href*="/jobs/view/"]')
    count = job_locators.count()
    print("Found job link elements (raw):", count)

    matches = []
    seen = set()
    # iterate through anchors
    for i in range(count):
        try:
            anchor = job_locators.nth(i)
            href = anchor.get_attribute("href")
            if not href or href in seen:
                continue
            seen.add(href)

            # Scroll anchor into view and click via JS to avoid opening new tab
            anchor.scroll_into_view_if_needed(timeout=2000)
            # small pause
            time.sleep(0.2)
            try:
                anchor.click(timeout=2000)
            except Exception:
                # fallback to JS click
                page.evaluate("(el) => el.click()", anchor)
            # Wait briefly for right panel to load
            time.sleep(1.0)

            # Extract description from right panel
            description = extract_description_from_right_panel(page)
            if not description:
                # if no description, skip
                continue

            if not description_matches(description, keywords_required, mode=match_mode, exclude=exclude_keywords):
                # doesn't match filters
                continue

            # grab metadata
            meta = extract_metadata_after_click(page, href)
            meta["description"] = description
            meta["search_location"] = location
            meta["search_work_type"] = work_type

            matches.append(meta)
            print("Matched:", meta["title"][:80], " - ", meta["company"][:60])

            if max_matches and len(matches) >= max_matches:
                break

            # polite pause
            time.sleep(0.5)
        except Exception as e:
            # ignore single failures and continue
            # print("item error:", e)
            continue

    return matches


def main():
    all_rows = []
    with sync_playwright() as p:
        browser_type = p.chromium
        # launch persistent context so existing cookies/session are reused
        launch_args = {}
        if BROWSER_CHANNEL:
            # use installed Chrome channel if requested
            try:
                browser_type = getattr(p, "chromium")
                launch_args["channel"] = BROWSER_CHANNEL
            except Exception:
                pass

        context = browser_type.launch_persistent_context(user_data_dir=PERSISTENT_PROFILE_DIR,
                                                         headless=False,
                                                         **launch_args)
        page = context.new_page()
        try:
            # Ensure user is logged in manually in that profile. If not logged in,
            # open LinkedIn and ask user to sign in (script will still run but won't access private jobs).
            # We won't attempt to programmatically log in.
            # Iterate searches
            for s in SEARCHES:
                matches = fetch_matches_for_search(page, s, KEYWORDS_REQUIRED, MATCH_MODE, EXCLUDE_KEYWORDS, MAX_MATCHES_PER_SEARCH)
                all_rows.extend(matches)
                # polite pause between searches
                time.sleep(1.2)

            # Save results
            if all_rows:
                df = pd.DataFrame(all_rows)
                df.to_csv(OUTPUT_CSV, index=False)
                print("Saved", len(all_rows), "matches to", OUTPUT_CSV)
            else:
                print("No matches found for the given criteria.")
        finally:
            # Keep browser open for inspection or close:
            context.close()


if __name__ == "__main__":
    main()
