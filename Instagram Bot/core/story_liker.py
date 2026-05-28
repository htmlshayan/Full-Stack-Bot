import logging
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from core.auth import human_delay

logger = logging.getLogger("model_dm_bot")


def _is_page_unavailable(driver) -> bool:
    try:
        title = str(driver.title or "").lower()
        if "page not found" in title:
            return True
        source = driver.page_source.lower()
        markers = [
            "sorry, this page isn't available",
            "the link you followed may be broken",
            "page may have been removed",
        ]
        return any(marker in source for marker in markers)
    except Exception:
        return False


def run_story_liker_script(
    driver,
    model_username,
    max_stories,
    include_highlights,
    max_highlights=None,
    stop_event=None,
    on_like=None,
):
    """
    Pure Selenium implementation of the Instagram Story Liker.
    Navigates to the model profile, opens stories/highlights, and likes stories.
    """
    total_liked = 0
    found_story = False

    def _normalize_limit(raw_value):
        try:
            limit = int(raw_value)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    story_limit = _normalize_limit(max_stories)
    highlight_limit = _normalize_limit(max_highlights)

    def _is_stop_requested():
        return bool(stop_event and stop_event.is_set())

    def _find_story_launch_element():
        selectors = [
            "header section div[role='button']",
            "header div[role='button'] img",
            "header canvas",
            "header a[href*='/stories/']",
            "header div[style*='border-radius']",
            "header [role='button']",
        ]

        script = """
            const selectors = arguments[0] || [];
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (!el) continue;
                let clickable = el;
                for (let i = 0; i < 5; i++) {
                    if (!clickable) break;
                    const role = clickable.getAttribute && clickable.getAttribute('role');
                    const tag = (clickable.tagName || '').toLowerCase();
                    const cursor = (clickable.style && clickable.style.cursor) || '';
                    if (role === 'button' || role === 'link' || tag === 'a' || tag === 'button' || cursor === 'pointer') {
                        return clickable;
                    }
                    clickable = clickable.parentElement;
                }
                if (el) return el;
            }

            var fallback = document.querySelector('a[href*="/stories/"]');
            if (fallback) return fallback;

            var aria = document.querySelector('[aria-label*="Story"], [aria-label*="story"]');
            if (aria) {
                var ariaBtn = aria.closest('[role=button], button, a');
                if (ariaBtn) return ariaBtn;
                return aria;
            }

            return null;
        """
        try:
            element = driver.execute_script(script, selectors)
            if element:
                return element
        except Exception:
            pass

        try:
            candidates = driver.find_elements(By.CSS_SELECTOR, "a[href*='/stories/']")
            if candidates:
                return candidates[0]
        except Exception:
            pass

        return None

    def _find_highlight_links():
        try:
            links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/stories/highlights/']")
        except Exception:
            links = []

        hrefs = []
        seen = set()
        for link in links:
            try:
                href = link.get_attribute("href")
            except Exception:
                href = None
            if not href:
                continue
            if href in seen:
                continue
            seen.add(href)
            hrefs.append(href)

        return hrefs

    def _find_highlight_elements():
        selectors = [
            "a[href*='/stories/highlights/']",
            "div[role='button'][class*='highlight']",
            "div[aria-label*='highlight' i]",
            "div[class*='storyHighlight']",
            "section div[role='button']",
        ]

        elements = []
        seen_ids = set()
        for selector in selectors:
            try:
                found = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                found = []

            for elem in found:
                try:
                    elem_id = elem.id
                except Exception:
                    elem_id = None

                if elem_id and elem_id in seen_ids:
                    continue

                label = str(elem.get_attribute("aria-label") or "").lower()
                href = str(elem.get_attribute("href") or "").lower()
                text = str(elem.text or "").lower()
                is_highlight = (
                    "/stories/highlights/" in href
                    or "highlight" in label
                    or "highlight" in text
                    or "story" in label
                    or "story" in text
                )
                if selector != "a[href*='/stories/highlights/']" and not is_highlight:
                    continue

                elements.append(elem)
                if elem_id:
                    seen_ids.add(elem_id)

        return elements

    def _find_story_button(label):
        labels = [label]
        if label == "Like":
            labels.append("Like story")
        if label == "Unlike":
            labels.append("Unlike story")
        if label == "Next":
            labels.append("Next story")
        if label == "Close":
            labels.append("Close")

        selectors = []
        for lbl in labels:
            selectors.append(f"svg[aria-label='{lbl}']")
            selectors.append(f"button[aria-label='{lbl}']")
            selectors.append(f"[role='button'][aria-label='{lbl}']")

        for selector in selectors:
            try:
                elems = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                elems = []

            for elem in elems:
                try:
                    if elem.tag_name.lower() == "svg":
                        btn = elem.find_element(By.XPATH, "./ancestor::*[@role='button'] | ./ancestor::button")
                        return btn
                    return elem
                except Exception:
                    return elem

        return None

    def _story_viewer_active():
        selectors = [
            "svg[aria-label='Like']",
            "svg[aria-label='Unlike']",
            "svg[aria-label='Next']",
            "svg[aria-label='Close']",
            "button[aria-label='Next']",
            "button[aria-label='Close']",
            "div[role='dialog'] svg[aria-label='Close']",
            "section[role='dialog']",
            "div[role='dialog']",
        ]

        for selector in selectors:
            try:
                if driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
            except Exception:
                continue

        return False

    def _wait_for_story_viewer(timeout_sec: float = 6.0) -> bool:
        end_time = time.time() + max(0.0, timeout_sec)
        while time.time() < end_time:
            if _story_viewer_active():
                return True
            human_delay(0.3, 0.6)
        return False

    def _safe_click(element) -> bool:
        if not element:
            return False
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        except Exception:
            pass

        try:
            element.click()
            return True
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                return False

    def _open_story_url(target_username: str) -> bool:
        story_url = f"https://www.instagram.com/stories/{target_username}/"
        driver.get(story_url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        human_delay(2.0, 2.6)
        return _wait_for_story_viewer()

    def _auto_open_story_viewer() -> bool:
        script = """
            const callback = arguments[arguments.length - 1];
            const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));

            const findSvgByLabel = (label) => (
                Array.from(document.querySelectorAll('svg'))
                    .find(svg => svg.getAttribute('aria-label') === label) || null
            );

            const hasViewer = () => {
                return Boolean(
                    findSvgByLabel('Like') ||
                    findSvgByLabel('Unlike') ||
                    findSvgByLabel('Close') ||
                    findSvgByLabel('Next') ||
                    document.querySelector('section[role="dialog"]') ||
                    document.querySelector('div[role="dialog"]')
                );
            };

            const findClickableParent = (el) => {
                let node = el;
                for (let i = 0; i < 6; i++) {
                    if (!node) break;
                    const role = node.getAttribute && node.getAttribute('role');
                    const tag = (node.tagName || '').toLowerCase();
                    const cursor = (node.style && node.style.cursor) || '';
                    if (role === 'button' || role === 'link' || tag === 'a' || tag === 'button' || cursor === 'pointer') {
                        return node;
                    }
                    node = node.parentElement;
                }
                return el || null;
            };

            const safeClick = (el) => {
                if (!el) return false;
                try {
                    el.scrollIntoView({ block: 'center' });
                } catch (e) {}
                try {
                    el.click();
                    return true;
                } catch (e) {
                    try {
                        const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                        el.dispatchEvent(evt);
                        return true;
                    } catch (err) {
                        return false;
                    }
                }
            };

            const dismissPopups = () => {
                const labels = [
                    'Not Now', 'Not now', 'Close', 'Allow all cookies', 'Accept',
                    'Only allow essential cookies', 'Save Info'
                ];
                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                for (const btn of buttons) {
                    const text = (btn.textContent || '').trim();
                    if (labels.includes(text)) {
                        try { btn.click(); } catch (e) {}
                    }
                }
            };

            (async () => {
                dismissPopups();

                const mainStorySelectors = [
                    'header section div[role="button"]',
                    'header div[role="button"] img',
                    'header canvas',
                    'header a[href*="/stories/"]',
                    'header div[style*="border-radius"]'
                ];

                for (const selector of mainStorySelectors) {
                    const element = document.querySelector(selector);
                    if (!element) continue;
                    const clickable = findClickableParent(element);
                    if (!safeClick(clickable)) continue;
                    await wait(1200);
                    if (hasViewer()) {
                        callback(true);
                        return;
                    }
                }

                const highlightSelectors = [
                    'a[href*="/stories/highlights/"]',
                    'div[role="button"][class*="highlight"]',
                    'div[aria-label*="highlight" i]',
                    'div[class*="storyHighlight"]',
                    'section div[role="button"]'
                ];

                const highlightSet = new Set();
                for (const selector of highlightSelectors) {
                    const nodes = document.querySelectorAll(selector);
                    nodes.forEach(node => highlightSet.add(node));
                }

                const highlights = Array.from(highlightSet);
                for (const element of highlights) {
                    const clickable = findClickableParent(element);
                    if (!safeClick(clickable)) continue;
                    await wait(1200);
                    if (hasViewer()) {
                        callback(true);
                        return;
                    }
                }

                callback(false);
            })().catch(() => callback(false));
        """

        try:
            result = driver.execute_async_script(script)
            return bool(result)
        except Exception:
            return False

    def _click_next_story():
        next_btn = _find_story_button("Next")
        if next_btn:
            try:
                next_btn.click()
                return True
            except Exception:
                pass

        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ARROW_RIGHT)
            return True
        except Exception:
            return False

    def _close_story_viewer():
        close_btn = _find_story_button("Close")
        if close_btn:
            try:
                close_btn.click()
            except Exception:
                pass
            return

        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ESCAPE)
        except Exception:
            pass

    def _process_story_viewer(max_items=None):
        nonlocal total_liked
        viewed = 0

        while True:
            if _is_stop_requested():
                break

            if max_items is not None and viewed >= max_items:
                break

            like_btn = _find_story_button("Like")
            unlike_btn = _find_story_button("Unlike")

            if like_btn:
                try:
                    like_btn.click()
                    total_liked += 1
                    if on_like:
                        try:
                            on_like(driver.current_url)
                        except Exception:
                            pass
                except Exception:
                    pass
            elif not unlike_btn:
                break

            viewed += 1
            human_delay(0.4, 0.8)

            if not _click_next_story():
                break

            human_delay(1.0, 1.6)
            if not _story_viewer_active():
                break

        return viewed

    try:
        profile_url = f"https://www.instagram.com/{model_username}/"
        logger.info(f"Story Liker: Navigating to profile @{model_username}")
        driver.get(profile_url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        human_delay(2.4, 3.6)
        if _is_page_unavailable(driver):
            logger.warning(f"⚠️ Profile @{model_username} unavailable. Skipping.")
            return {"total_liked": 0, "found_story": False}
        try:
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

        story_button = _find_story_launch_element()
        remaining = story_limit

        story_opened = False
        if story_button:
            story_opened = _safe_click(story_button)

        if story_opened and not _wait_for_story_viewer():
            story_opened = False

        if not story_opened:
            story_opened = _auto_open_story_viewer()

        if story_opened and not _wait_for_story_viewer():
            story_opened = False

        if not story_opened:
            try:
                story_opened = _open_story_url(model_username)
            except Exception:
                story_opened = False

        if story_opened and _wait_for_story_viewer():
            found_story = True
            viewed = _process_story_viewer(remaining)
            if remaining is not None:
                remaining = max(0, remaining - viewed)
            _close_story_viewer()
            human_delay(1.0, 1.4)

        if include_highlights and not _is_stop_requested():
            if remaining is None or remaining > 0:
                driver.get(profile_url)
                human_delay(2.0, 3.0)

                highlights = _find_highlight_links()
                highlight_elements = [] if highlights else _find_highlight_elements()
                if highlight_limit is not None:
                    highlights = highlights[:highlight_limit]

                if highlights or highlight_elements:
                    found_story = True

                if highlights:
                    for href in highlights:
                        if _is_stop_requested():
                            break
                        if remaining is not None and remaining <= 0:
                            break

                        driver.get(href)
                        human_delay(2.0, 2.8)

                        if not _wait_for_story_viewer():
                            continue

                        viewed = _process_story_viewer(remaining)
                        if remaining is not None:
                            remaining = max(0, remaining - viewed)
                        _close_story_viewer()
                        human_delay(1.0, 1.4)
                else:
                    for elem in highlight_elements:
                        if _is_stop_requested():
                            break
                        if remaining is not None and remaining <= 0:
                            break

                        if not _safe_click(elem):
                            continue

                        human_delay(2.0, 2.8)
                        if not _wait_for_story_viewer():
                            continue

                        viewed = _process_story_viewer(remaining)
                        if remaining is not None:
                            remaining = max(0, remaining - viewed)
                        _close_story_viewer()
                        human_delay(1.0, 1.4)

        return {"total_liked": total_liked, "found_story": found_story}

    except Exception as e:
        logger.error(f"Story Liker error for @{model_username}: {e}")
        return {"total_liked": total_liked, "found_story": found_story}
