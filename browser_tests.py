"""
Random Browser Session — Selenium Learning Project
Website: https://www.abdulsamad.shop

This script demonstrates browser navigation, internal-link discovery,
randomized scrolling, timed sessions, action counting, and shutdown.

Automated advertising clicks are disabled.
"""

import random
import time
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException


# ============================== SETTINGS =====================================

BASE_URL = "https://www.abdulsamad.shop"
ALLOWED_HOSTS = {"abdulsamad.shop", "www.abdulsamad.shop"}

MIN_SESSION_SECONDS = 60
MAX_SESSION_SECONDS = 120

ACTION_LIMIT = 1000
WAIT_AFTER_LIMIT_SECONDS = 3600  # One hour

PAGE_LOAD_TIMEOUT_SECONDS = 30

FALLBACK_PAGES = [
    f"{BASE_URL}/fonts/bold",
    f"{BASE_URL}/fonts/italic",
    f"{BASE_URL}/fonts/script",
    f"{BASE_URL}/symbols",
    f"{BASE_URL}/emoji",
    f"{BASE_URL}/kaomoji",
    f"{BASE_URL}/?category=bold",
    f"{BASE_URL}/?category=cursive",
    f"{BASE_URL}/styles",
    f"{BASE_URL}/about",
]


# ============================== URL HELPERS ==================================

def is_internal_url(url):
    """Check whether a URL belongs to the configured website."""
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.hostname.lower() in ALLOWED_HOSTS
        )

    except (TypeError, ValueError):
        return False


def normalize_url(url):
    """Remove URL fragments and reject external URLs."""
    if not is_internal_url(url):
        return None

    parsed = urlparse(url)
    normalized = parsed._replace(fragment="").geturl()

    if normalized.rstrip("/") == BASE_URL.rstrip("/"):
        return BASE_URL

    return normalized


# ============================== LINK DISCOVERY ===============================

def get_all_internal_links(driver):
    """Collect unique internal links from the current page."""
    links = set()

    try:
        anchors = driver.find_elements(By.TAG_NAME, "a")

        for anchor in anchors:
            try:
                href = anchor.get_attribute("href")
                normalized = normalize_url(href)

                if normalized and normalized != BASE_URL:
                    links.add(normalized)

            except WebDriverException:
                continue

    except WebDriverException as error:
        print(f"Could not collect links: {error}")

    return sorted(links)


# ============================== PAGE SCROLLING ===============================

def scroll_for_duration(driver, duration, deadline):
    """Scroll for a limited time without intentionally exceeding the deadline."""
    end_time = min(time.monotonic() + duration, deadline)

    print(f"Scrolling for up to {duration:.1f} seconds...")

    while time.monotonic() < end_time:
        try:
            amount = random.randint(250, 700)

            driver.execute_script(
                "window.scrollBy(0, arguments[0]);",
                amount,
            )

            if random.random() < 0.18:
                upward_amount = random.randint(80, 250)

                driver.execute_script(
                    "window.scrollBy(0, arguments[0]);",
                    -upward_amount,
                )

        except WebDriverException as error:
            print(f"Scrolling stopped: {error}")
            break

        remaining = end_time - time.monotonic()

        if remaining <= 0:
            break

        time.sleep(min(random.uniform(0.7, 1.5), remaining))

    print("Scrolling finished.")


# ============================== PAGE NAVIGATION ==============================

def visit_random_page(driver, pages, deadline):
    """Visit a random internal page and return whether navigation succeeded."""
    if not pages or time.monotonic() >= deadline:
        return False

    target = random.choice(pages)
    print(f"Visiting: {target}")

    try:
        driver.get(target)

    except TimeoutException:
        print("Page-load timeout; checking whether the page is usable.")

    except WebDriverException as error:
        print(f"Navigation failed: {error}")
        return False

    try:
        print(f"Page title: {driver.title or '(no title)'}")
    except WebDriverException:
        pass

    if time.monotonic() < deadline:
        scroll_for_duration(
            driver,
            random.uniform(3, 8),
            deadline,
        )

    # Occasionally discover more internal links.
    if random.random() < 0.3 and time.monotonic() < deadline:
        for link in get_all_internal_links(driver):
            if link not in pages:
                pages.append(link)

    return True


# ================================== MAIN =====================================

def main():
    options = Options()

    # Uncomment to maximize the Chrome window:
    # options.add_argument("--start-maximized")

    driver = None
    total_actions = 0

    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)

        print(f"Starting browser session for {BASE_URL}")
        print("Advertising clicks are disabled.")
        print("Press Ctrl+C to stop.\n")

        while True:

            # Pause when the navigation-action limit is reached.
            if total_actions >= ACTION_LIMIT:
                print(
                    f"\nReached {ACTION_LIMIT} navigation actions. "
                    f"Pausing for {WAIT_AFTER_LIMIT_SECONDS} seconds."
                )

                time.sleep(WAIT_AFTER_LIMIT_SECONDS)
                total_actions = 0

                print("Pause complete. Resuming.\n")

            session_seconds = random.uniform(
                MIN_SESSION_SECONDS,
                MAX_SESSION_SECONDS,
            )

            deadline = time.monotonic() + session_seconds

            print(
                f"\nNew session: {session_seconds:.1f} seconds "
                f"| Actions: {total_actions}"
            )

            # Open the home page.
            try:
                driver.get(BASE_URL)

            except TimeoutException:
                print("Home page timed out; continuing if possible.")

            except WebDriverException as error:
                print(f"Could not open the home page: {error}")
                time.sleep(5)
                continue

            try:
                print(f"Home page title: {driver.title or '(no title)'}")
            except WebDriverException:
                pass

            # Discover pages.
            pages = get_all_internal_links(driver)

            if not pages:
                print("No internal links found; using fallback URLs.")

                pages = [
                    page for page in FALLBACK_PAGES
                    if is_internal_url(page)
                ]

            print(f"Available internal pages: {len(pages)}")

            # Run the current session.
            while (
                time.monotonic() < deadline
                and total_actions < ACTION_LIMIT
            ):
                succeeded = visit_random_page(
                    driver,
                    pages,
                    deadline,
                )

                if succeeded:
                    total_actions += 1

                    print(
                        f"Navigation actions completed: {total_actions}"
                    )

                remaining = deadline - time.monotonic()

                if remaining > 0:
                    time.sleep(
                        min(
                            random.uniform(1.0, 2.0),
                            remaining,
                        )
                    )

            print(
                f"Session finished. Total actions: {total_actions}"
            )

    except KeyboardInterrupt:
        print("\nStopped by user.")

    except WebDriverException as error:
        print(f"\nBrowser error: {error}")
        print(
            "Check Chrome installation and update Selenium with:\n"
            "python -m pip install --upgrade selenium"
        )

    finally:
        if driver is not None:
            print("Closing browser...")

            try:
                driver.quit()
            except WebDriverException:
                pass

        print("Browser session ended.")


if __name__ == "__main__":
    main()