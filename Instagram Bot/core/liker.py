import os
import time
import json
import logging
import random
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from core.auth import human_delay, human_scroll

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

def run_comment_liker_script(driver, model_username, posts_count, likes_per_post, stop_event=None, on_like=None):
    """
    Pure Selenium implementation of the Instagram Comment Liker.
    Navigates to the model profile and likes comments on posts/reels.
    """
    total_liked = 0
    found_posts = False

    def _normalize_limit(raw_value):
        try:
            limit = int(raw_value)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    posts_limit = _normalize_limit(posts_count)
    likes_limit = _normalize_limit(likes_per_post)

    def _collect_post_urls():
        post_urls = []
        seen = set()
        stagnant_rounds = 0
        last_count = 0

        while True:
            if stop_event and stop_event.is_set():
                break

            try:
                links = driver.find_elements(
                    By.XPATH,
                    "//a[contains(@href, '/p/') or contains(@href, '/reel/')]")
            except Exception:
                links = []

            for el in links:
                try:
                    href = el.get_attribute("href")
                except Exception:
                    href = None

                if not href:
                    continue
                if "/p/" not in href and "/reel/" not in href:
                    continue
                if href in seen:
                    continue

                seen.add(href)
                post_urls.append(href)
                if posts_limit and len(post_urls) >= posts_limit:
                    return post_urls

            if len(post_urls) == last_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
                last_count = len(post_urls)

            if posts_limit and len(post_urls) >= posts_limit:
                break
            if stagnant_rounds >= 3:
                break

            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
            human_delay(1.8, 2.6)

        return post_urls

    def _load_all_comments():
        idle_rounds = 0
        max_rounds = 200  # Safety cap to prevent infinite load loops.

        for _ in range(max_rounds):
            if stop_event and stop_event.is_set():
                return

            clicked = False
            try:
                load_more_svgs = driver.find_elements(
                    By.CSS_SELECTOR,
                    'svg[aria-label="Load more comments"], svg[aria-label="View replies"]',
                )
            except Exception:
                load_more_svgs = []

            for svg in load_more_svgs:
                try:
                    parent_btn = svg.find_element(
                        By.XPATH,
                        "./ancestor::div[@role='button'] | ./ancestor::button",
                    )
                    driver.execute_script(
                        "arguments[0].scrollIntoView({ behavior: 'smooth', block: 'center' });",
                        parent_btn,
                    )
                    human_delay(0.4, 0.8)
                    driver.execute_script("arguments[0].click();", parent_btn)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                try:
                    view_all = driver.execute_script(
                        """
                        return Array.from(document.querySelectorAll('div[role="button"], button, span'))
                            .find(el => el.textContent
                                && (el.textContent.includes('View all') || el.textContent.includes('View more'))
                                && el.textContent.includes('comments'));
                        """
                    )
                except Exception:
                    view_all = None

                if view_all:
                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({ behavior: 'smooth', block: 'center' });",
                            view_all,
                        )
                        human_delay(0.4, 0.8)
                        driver.execute_script("arguments[0].click();", view_all)
                        clicked = True
                    except Exception:
                        clicked = False

            if clicked:
                idle_rounds = 0
                human_delay(1.2, 2.0)
            else:
                idle_rounds += 1
                if idle_rounds >= 3:
                    break
                human_delay(0.6, 1.0)

    def _find_unliked_comment_buttons():
        try:
            like_svgs = driver.find_elements(By.CSS_SELECTOR, 'svg[aria-label="Like"]')
        except Exception:
            like_svgs = []

        buttons = []
        for svg in like_svgs:
            try:
                h = svg.get_attribute("height") or svg.size.get("height")
                if h and int(float(h)) > 20:
                    continue
                btn = svg.find_element(
                    By.XPATH,
                    "./ancestor::div[@role='button'] | ./ancestor::button",
                )
                buttons.append(btn)
            except Exception:
                continue

        return buttons
    
    try:
        # 1. Navigate to model profile
        profile_url = f"https://www.instagram.com/{model_username}/"
        logger.info(f"🚀 Navigating to model profile: @{model_username}")
        driver.get(profile_url)
        human_delay(3, 5)
        if _is_page_unavailable(driver):
            logger.warning(f"⚠️ Profile @{model_username} unavailable. Skipping.")
            return {"total_liked": 0, "found_posts": False}

        # 2. Find posts/reels
        WebDriverWait(driver, 15).until(
            EC.presence_of_all_elements_located(
                (By.XPATH, "//a[contains(@href, '/p/') or contains(@href, '/reel/')]")
            )
        )

        post_urls = _collect_post_urls()

        if not (stop_event and stop_event.is_set()):
            if posts_limit is None or len(post_urls) < posts_limit:
                reels_url = f"https://www.instagram.com/{model_username}/reels/"
                try:
                    driver.get(reels_url)
                    human_delay(3, 5)
                    reels_urls = _collect_post_urls()
                except Exception:
                    reels_urls = []

                if reels_urls:
                    merged = []
                    seen = set()
                    for url in post_urls + reels_urls:
                        if url in seen:
                            continue
                        seen.add(url)
                        merged.append(url)
                    post_urls = merged

        if posts_limit is not None and len(post_urls) > posts_limit:
            post_urls = post_urls[:posts_limit]

        if not post_urls:
            logger.warning(f"⚠️ No posts/reels found on @{model_username}'s profile.")
            return {"total_liked": 0, "found_posts": False}

        found_posts = True

        logger.info(f"📸 Found {len(post_urls)} posts/reels to check on @{model_username}")

        # 3. Process each post
        for i, post_url in enumerate(post_urls):
            if stop_event and stop_event.is_set(): break
            
            logger.info(f"📖 Checking post {i+1}/{len(post_urls)}: {post_url}")
            driver.get(post_url)
            human_delay(4, 6) # Give it time to load

            likes_on_this_post = 0

            # --- Loading Comments Loop ---
            _load_all_comments()

            # --- Liking Comments (All) ---
            logger.info("🔍 Searching for comments to like...")

            while True:
                if stop_event and stop_event.is_set():
                    break
                if likes_limit is not None and likes_on_this_post >= likes_limit:
                    break

                comment_buttons = _find_unliked_comment_buttons()
                if not comment_buttons:
                    logger.info("🏁 No more unliked comments visible on this post.")
                    break

                clicked_any = False
                for target_btn in comment_buttons:
                    if stop_event and stop_event.is_set():
                        break
                    if likes_limit is not None and likes_on_this_post >= likes_limit:
                        break

                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({ behavior: 'smooth', block: 'center' });",
                            target_btn,
                        )
                        human_delay(0.4, 0.8)
                        driver.execute_script("arguments[0].click();", target_btn)

                        likes_on_this_post += 1
                        total_liked += 1
                        clicked_any = True
                        logger.info(f"❤️ Liked a comment ({likes_on_this_post})")

                        if on_like:
                            try:
                                on_like(driver.current_url)
                            except Exception:
                                pass

                        human_delay(1.2, 2.2)
                    except Exception as e:
                        logger.warning(f"⚠️ Skipping a comment due to interaction error: {e}")
                        human_delay(0.8, 1.4)
                        continue

                if not clicked_any:
                    break

            logger.info(f"✅ Finished post {i+1}. Total likes given so far: {total_liked}")
            human_delay(2, 4)

    except Exception as e:
        logger.error(f"❌ Selenium Liker Error for @{model_username}: {e}")
        if not found_posts:
            found_posts = True
        
    return {"total_liked": total_liked, "found_posts": found_posts}
