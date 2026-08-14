import html as html_module
import json
import logging
import msvcrt
import re
import shutil
import smtplib
import sys
import time
import traceback
import warnings
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD
from config import VAULT_ROOT as VAULT_ROOT_STR

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


LOG_FILE = Path(__file__).parent / "emailme_watcher.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("emailme")

# ---------------------------------------------------------------------------
# CONFIG - fill these in before running
# ---------------------------------------------------------------------------

VAULT_ROOT = Path(VAULT_ROOT_STR)
ARCHIVE_DIR = VAULT_ROOT / "archive"

EMAIL_TO = GMAIL_ADDRESS  # sending to yourself; change if different

# Normal browser User-Agent, so sites don't serve interstitial/verification
# pages to an obvious script (this was causing the Reddit/aweber title bug).
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# A note only stops being auto-retried once it's failed 3 separate runs;
# at that point finalize_note() renames it with this IGNORED marker and it's
# never picked up again automatically. Everything else in root - fresh
# captures, notes mid-retry (_FAILED.1./_FAILED.2.), and archived notes
# someone's manually moved back for a rerun - gets picked up on every scan.
IGNORED_PATTERN = re.compile(
    r"^_FAILED\.IGNORED\.\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(_\d+)?_(TXT|URL|YT|IMG)\.md$"
)

FAILED_ATTEMPT_PATTERN = re.compile(r"^_FAILED\.(\d+)\.")

MAX_RETRY_ATTEMPTS = 3


def get_retry_attempt(path: Path) -> int:
    """
    How many times this note has already failed, per its filename
    (_FAILED.<n>.<original-timestamp>_<TAG>.md). 0 for a fresh capture, a
    reprocessed archive move-back, or anything else with no _FAILED.<n>.
    prefix.
    """
    match = FAILED_ATTEMPT_PATTERN.match(path.name)
    return int(match.group(1)) if match else 0

URL_PATTERN = re.compile(r"https?://[^\s)\]]+")
YOUTUBE_PATTERN = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# ---------------------------------------------------------------------------
# STEP 1 - find unprocessed notes
# ---------------------------------------------------------------------------

TIMESTAMP_EXTRACT = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})")


def extract_timestamp(path: Path):
    """
    Pull the capture timestamp out of a filename (format: yyyy.mm.dd_hh.mm.ss,
    as written by Tasker and the Firefox native host), so notes can be sorted
    by when they were actually captured, not by raw filename text. Falls
    back to the file's own modified time if no timestamp is found in the
    name (e.g. a synthesized orphan-image note).
    """
    match = TIMESTAMP_EXTRACT.search(path.name)
    if match:
        return datetime(*map(int, match.groups()))
    return datetime.fromtimestamp(path.stat().st_mtime)


def find_unprocessed_notes():
    """
    Return a list of Path objects for .md files in VAULT_ROOT (not archive/)
    that aren't permanently given-up-on (see IGNORED_PATTERN) - this
    includes new captures, notes still eligible for a retry, and any note
    manually moved back from archive/ for a rerun. Sorted by actual
    capture timestamp, so a batch of shares gets emailed and archived in
    the order they were originally shared, not filesystem/alphabetical
    order.
    """
    notes = []
    for path in VAULT_ROOT.glob("*.md"):
        if not IGNORED_PATTERN.match(path.name):
            notes.append(path)
    notes.sort(key=extract_timestamp)
    return notes


# ---------------------------------------------------------------------------
# STEP 2 - classify what's in a note
# ---------------------------------------------------------------------------


def classify_note(text: str, has_image: bool):
    """
    Look at the note's text and whether an embedded image was found.
    Returns a dict describing what's present, and the TYPE tag per your
    tier system (TEXT < LINK < IMAGE, tag = highest tier present).
    """
    urls = URL_PATTERN.findall(text)
    has_link = len(urls) > 0
    has_youtube = any(YOUTUBE_PATTERN.search(u) for u in urls)
    body_text = text.strip()
    text_without_links = URL_PATTERN.sub("", text).strip()
    has_extra_text = len(text_without_links) > 0
    has_text = len(body_text) > 0

    if has_image:
        tag = "IMG"
    elif has_youtube:
        tag = "YT"
    elif has_link:
        tag = "URL"
    else:
        tag = "TXT"

    return {
        "urls": urls,
        "body_text": body_text,
        "has_extra_text": has_extra_text,
        "has_image": has_image,
        "tag": tag,
    }


def find_embedded_image(note_path: Path):
    """
    Obsidian embeds images in a note as ![[filename.png]] or ![](filename.png).
    Look for that syntax, and return the resolved Path to the image file if found,
    else None.

    NOTE: this assumes images land in the same folder as the note, or in a
    vault-standard attachments folder. Confirm where Obsidian's Android share
    actually puts image attachments in your vault before trusting this -
    may need adjusting once we test with a real image share.
    """
    text = note_path.read_text(encoding="utf-8")
    match = re.search(r"!\[\[(.+?)\]\]|!\[.*?\]\((.+?)\)", text)
    if not match:
        return None
    filename = match.group(1) or match.group(2)
    candidate = note_path.parent / filename
    if candidate.exists():
        return candidate
    matches = list(VAULT_ROOT.parent.rglob(filename))
    return matches[0] if matches else None


def find_orphan_images(claimed_images: set):
    """
    Find image files sitting directly in VAULT_ROOT that no unprocessed
    .md note currently embeds. claimed_images is a set of resolved Paths
    already referenced by a note this run.
    """
    orphans = []
    for path in VAULT_ROOT.iterdir():
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            if path.resolve() not in claimed_images:
                orphans.append(path)
    return orphans


# ---------------------------------------------------------------------------
# STEP 3 - enrich links
# ---------------------------------------------------------------------------


def discover_and_scrape_rss(page_url: str, page_soup):
    """
    General (non-Reddit) RSS fallback. Auto-discover only: only uses a
    feed if the page's own HTML explicitly advertises one via a
    <link rel="alternate" type="application/rss+xml" or atom+xml"> tag.
    No guessing at common paths like /feed or /rss on sites that don't
    declare one. Returns None if no feed is found or nothing usable
    comes back from it.
    """
    feed_tag = page_soup.find(
        "link", rel="alternate", type=re.compile(r"(rss|atom)\+xml")
    )
    if not feed_tag or not feed_tag.get("href"):
        return None

    feed_url = feed_tag["href"]
    if feed_url.startswith("/"):
        parsed = urlparse(page_url)
        feed_url = f"{parsed.scheme}://{parsed.netloc}{feed_url}"

    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        feed_soup = BeautifulSoup(resp.text, "xml")

        entry = feed_soup.find("entry") or feed_soup.find("item")
        if not entry:
            return None

        entry_link = None
        link_tag = entry.find("link")
        if link_tag:
            entry_link = link_tag.get("href") or (
                str(link_tag.string) if link_tag.string else None
            )

        if entry_link and entry_link.rstrip("/") != page_url.rstrip("/"):
            return None

        title_tag = entry.find("title")
        title = str(title_tag.string) if title_tag and title_tag.string else None
        if not title:
            return None

        description = None
        desc_tag = (
            entry.find("description") or entry.find("summary") or entry.find("content")
        )
        if desc_tag and desc_tag.string:
            desc_soup = BeautifulSoup(str(desc_tag.string), "html.parser")
            text = desc_soup.get_text(separator=" ", strip=True)
            description = text[:300] if text else None

        return {
            "final_url": page_url,
            "title": title,
            "description": description,
            "image_url": None,
        }
    except Exception as e:
        log.warning(f"General RSS fallback failed: {e}")
        return None


def fetch_with_backoff(url: str, headers: dict, timeout: int, max_attempts: int = 3):
    """
    Fetches a URL, retrying with increasing delay only if actually
    rate-limited (429) or hit with a server error (5xx). No delay at all
    on the first attempt or on a normal success/4xx-other-than-429, so
    Reddit links that aren't currently being throttled cost nothing extra.
    """
    delay = 3
    resp = None
    for attempt in range(1, max_attempts + 1):
        resp = requests.get(url, allow_redirects=True, timeout=timeout, headers=headers)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < max_attempts:
                log.info(
                    f"Got {resp.status_code}, retrying in {delay}s (attempt {attempt}/{max_attempts})..."
                )
                time.sleep(delay)
                delay *= 2
                continue
        return resp
    return resp


META_REFRESH_PATTERN = re.compile(
    r'content=["\']?\s*\d+\s*;\s*url=([^"\'>]+)', re.IGNORECASE
)
JS_REDIRECT_PATTERN = re.compile(
    r'location(?:\.href)?\s*=\s*["\']([^"\']+)["\']'
    r'|location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
    re.IGNORECASE,
)


def follow_client_side_redirect(resp, headers: dict, max_hops: int = 3):
    """
    requests only follows real HTTP 3xx redirects. Some click-tracking links
    (e.g. Substack's) return a 200 with a client-side redirect instead - a
    <meta http-equiv="refresh"> tag or a JS location.href assignment, kept as
    a fallback for email clients that render HTML but block JavaScript - and
    requests has no way to see either. This checks for both patterns and
    follows them manually, up to max_hops times, so those links resolve to
    their real destination instead of stalling on the tracking page.

    NOTE: built against the standard technique these services use, not
    verified against a live Substack redirect page directly (fetch attempts
    were rate-limited while writing this). Confirm it actually resolves
    before relying on it.
    """
    for _ in range(max_hops):
        match = META_REFRESH_PATTERN.search(resp.text)
        next_url = match.group(1).strip().strip("\"'") if match else None
        if not next_url:
            js_match = JS_REDIRECT_PATTERN.search(resp.text)
            if js_match:
                next_url = js_match.group(1) or js_match.group(2)
        if not next_url:
            break
        if next_url.startswith("/"):
            parsed = urlparse(resp.url)
            next_url = f"{parsed.scheme}://{parsed.netloc}{next_url}"
        try:
            resp = requests.get(
                next_url, allow_redirects=True, timeout=10, headers=headers
            )
        except requests.RequestException as e:
            log.warning(f"Client-side redirect follow failed: {e}")
            break
    return resp


def resolve_and_scrape(url: str):
    """
    Follow redirects to the final URL, then hand off to the right scraper
    for what kind of link it turned out to be. Returns a dict:
    {final_url, title, description, image_url} - any field may be None if
    not found. Reddit and generic-site handling live in their own
    functions (scrape_reddit_link, scrape_generic_link) since each is its
    own multi-layer fallback cascade.
    """
    if "reddit.com" in url:
        resp = fetch_with_backoff(url, headers=HEADERS, timeout=10)
    else:
        resp = requests.get(url, allow_redirects=True, timeout=10, headers=HEADERS)

    resp = follow_client_side_redirect(resp, HEADERS)
    final_url = resp.url

    if "youtube.com" in final_url or "youtu.be" in final_url:
        yt_data = scrape_youtube_oembed(final_url)
        if yt_data.get("title"):
            return yt_data

    if "reddit.com" in final_url:
        return scrape_reddit_link(final_url, resp)

    return scrape_generic_link(final_url, resp)


def scrape_reddit_link(final_url: str, resp):
    """
    Reddit-specific 4-layer fallback cascade, in order of richest to
    poorest result: OG scraping (when Reddit isn't currently blocking the
    request), Reddit's own RSS/Atom feed, Reddit's JSON endpoint, and
    finally parsing a title straight from the URL slug with no network
    request at all. Each layer is a real attempt, not a hardcoded
    fallback, so this self-heals automatically if Reddit's blocking
    changes. Always returns a dict, worst case with every field None.
    """
    # Layer 1: real OG scraping, richest result when Reddit isn't
    # currently blocking the request
    soup = BeautifulSoup(resp.text, "html.parser")

    def og(prop):
        tag = soup.find("meta", property=f"og:{prop}")
        return tag["content"] if tag and tag.get("content") else None

    og_title = og("title")
    if (
        og_title
        and "please wait" not in og_title.lower()
        and "just a moment" not in og_title.lower()
    ):
        raw_description = og("description")
        author = None
        if raw_description:
            submitted_match = re.search(
                r"submitted by /?(u/\S+?)(?:\s*\[link\]|\s*to\s|\s*$)",
                raw_description,
            )
            if submitted_match:
                author = submitted_match.group(1)

        subreddit_from_desc = None
        subreddit_match = REDDIT_URL_PATTERN.search(final_url)
        if subreddit_match:
            subreddit_from_desc = f"r/{subreddit_match.group(1)}"

        log.debug(f"Reddit raw description: {raw_description!r}")
        log.debug(f"Reddit parsed author={author!r} subreddit={subreddit_from_desc!r}")

        comments_url = final_url
        og_url_tag = og("url")
        link_url = og_url_tag if og_url_tag and og_url_tag != final_url else None

        log.info("Reddit method used: OG scrape")

        return {
            "final_url": final_url,
            "title": og_title,
            "description": None,
            "image_url": og("image"),
            "reddit_author": author,
            "reddit_subreddit": subreddit_from_desc,
            "reddit_comments_url": comments_url,
            "reddit_link_url": link_url,
            "reddit_method": "OG scrape",
        }

    # Layer 2: Reddit's RSS/Atom feed, currently getting past the block
    # that JSON and generic scraping both hit
    rss_data = scrape_reddit_rss(final_url)
    if rss_data and rss_data.get("title"):
        log.info("Reddit method used: RSS")
        rss_data["reddit_method"] = "RSS"
        return rss_data

    # Layer 3: Reddit's JSON endpoint, kept as a real attempt so it
    # self-heals automatically if Reddit's block ever lifts
    json_data = scrape_reddit_json(final_url)
    if (
        json_data
        and json_data.get("title")
        and not json_data["title"].endswith(")")
    ):
        log.info("Reddit method used: JSON")
        json_data["reddit_method"] = "JSON"
        return json_data
        # scrape_reddit_json already has its own slug-parsing fallback
        # baked in (title ending in "(r/subreddit)"); skip that internal
        # fallback result here so layer 4 below is the single, final
        # slug-parsing attempt rather than running it twice

    # Layer 4: parse the title straight from the URL slug, no network
    # request, works even when everything above fails
    match = REDDIT_URL_PATTERN.search(final_url)
    if match:
        subreddit, slug = match.groups()
        title = slug.replace("_", " ").strip().lower()
        if title:
            log.info("Reddit method used: slug parsing")
            return {
                "final_url": final_url,
                "title": f"{title} (r/{subreddit})",
                "description": None,
                "image_url": None,
                "reddit_method": "slug parsing",
            }
    log.warning("Reddit method used: all methods failed")
    return {
        "final_url": final_url,
        "title": None,
        "description": None,
        "image_url": None,
        "reddit_method": "all methods failed",
    }


def scrape_generic_link(final_url: str, resp):
    """
    Non-Reddit, non-YouTube link handling: try the page's own og: meta
    tags first, fall back to its plain <title> tag, and if neither gives
    a title at all, try the RSS/Atom-autodiscovery fallback
    (discover_and_scrape_rss) before giving up with every field None.
    """
    soup = BeautifulSoup(resp.text, "html.parser")

    def og(prop):
        tag = soup.find("meta", property=f"og:{prop}")
        return tag["content"] if tag and tag.get("content") else None

    og_title = og("title") or (
        str(soup.title.string) if soup.title and soup.title.string else None
    )
    if og_title:
        return {
            "final_url": final_url,
            "title": og_title,
            "description": og("description"),
            "image_url": og("image"),
        }

    # No OG title found at all; see if the page advertises an RSS/Atom
    # feed and try to pull a matching entry from that instead
    rss_result = discover_and_scrape_rss(final_url, soup)
    if rss_result:
        return rss_result

    return {
        "final_url": final_url,
        "title": None,
        "description": None,
        "image_url": None,
    }




def extract_youtube_full_description(page_html: str):
    """
    YouTube truncates its og:description meta tag to roughly 160 characters
    (a common convention for social-preview tags). The real, full video
    description lives inside a large embedded JSON blob called
    ytInitialData in the page's own <script> tags, the same data YouTube's
    own page JavaScript uses to render the "Show more" description box.
    This pulls that blob out and walks its structure to find the full text.
    Returns None if the expected structure isn't found (YouTube changes
    this internal layout occasionally without notice).
    """
    match = re.search(r"var ytInitialData = (\{.*?\});</script>", page_html, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    try:
        contents = data["contents"]["twoColumnWatchNextResults"]["results"]["results"][
            "contents"
        ]
        for item in contents:
            video_secondary_info = item.get("videoSecondaryInfoRenderer")
            if video_secondary_info:
                description_obj = video_secondary_info.get(
                    "attributedDescription"
                ) or video_secondary_info.get("description")
                if description_obj and description_obj.get("content"):
                    full_text = description_obj["content"]
                    log.info(f"YouTube full description extracted: {len(full_text)} chars")
                    return full_text
                if description_obj and description_obj.get("runs"):
                    full_text = "".join(
                        run.get("text", "") for run in description_obj["runs"]
                    )
                    log.info(f"YouTube full description extracted: {len(full_text)} chars")
                    return full_text
    except (KeyError, IndexError, TypeError):
        pass
    log.info(
        "YouTube full description extraction found nothing, will fall back to short og:description"
    )
    return None


def extract_youtube_metadata(page_html: str):
    """
    View count, upload date, and duration live in a second embedded JSON
    blob, ytInitialPlayerResponse (separate from ytInitialData, which is
    used for the description). Pulls videoDetails.viewCount,
    microformat.playerMicroformatRenderer.publishDate, and
    videoDetails.lengthSeconds out of it.
    Returns a dict with any/all of "view_count", "upload_date", "duration"
    set to None if not found. Never raises - these are nice-to-haves.
    """
    result = {"view_count": None, "upload_date": None, "duration": None}

    match = re.search(
        r"var ytInitialPlayerResponse\s*=\s*(\{.*?\});\s*(?:var |</script>)",
        page_html,
        re.DOTALL,
    )
    if not match:
        return result

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return result

    video_details = data.get("videoDetails", {})

    raw_view_count = video_details.get("viewCount")
    if raw_view_count and raw_view_count.isdigit():
        result["view_count"] = f"{int(raw_view_count):,} views"

    raw_publish_date = (
        data.get("microformat", {})
        .get("playerMicroformatRenderer", {})
        .get("publishDate")
    )
    if raw_publish_date:
        try:
            dt = datetime.strptime(raw_publish_date, "%Y-%m-%d")
            result["upload_date"] = f"{dt.strftime('%b')} {dt.day}, {dt.year}"
        except ValueError:
            pass

    raw_length = video_details.get("lengthSeconds")
    if raw_length and raw_length.isdigit():
        total_seconds = int(raw_length)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            result["duration"] = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            result["duration"] = f"{minutes}:{seconds:02d}"

    return result


def scrape_youtube_oembed(url: str):
    """
    YouTube's oEmbed endpoint gives title, thumbnail, and channel name, but
    has no description field at all. To get a real description, also fetch
    the video page directly and pull its og:description tag.
    """
    api = f"https://www.youtube.com/oembed?url={quote(url, safe='')}&format=json"
    resp = requests.get(api, timeout=10, headers=HEADERS)

    if resp.status_code != 200:
        return {
            "final_url": url,
            "title": None,
            "description": None,
            "image_url": None,
            "channel": None,
        }
    data = resp.json()

    description = None
    metadata = {"view_count": None, "upload_date": None, "duration": None}
    try:
        page_resp = requests.get(url, timeout=10, headers=HEADERS)
        description = extract_youtube_full_description(page_resp.text)
        if not description:
            # fall back to the short og:description if the full-text
            # extraction didn't find anything usable
            page_soup = BeautifulSoup(page_resp.text, "html.parser")
            desc_tag = page_soup.find("meta", property="og:description")
            if desc_tag and desc_tag.get("content"):
                description = desc_tag["content"]
        metadata = extract_youtube_metadata(page_resp.text)
    except Exception:
        pass  # description/metadata are nice-to-haves; don't fail the whole capture over them

    return {
        "final_url": url,
        "title": data.get("title"),
        "description": description,
        "image_url": data.get("thumbnail_url"),
        "channel": data.get("author_name"),
        "view_count": metadata["view_count"],
        "upload_date": metadata["upload_date"],
        "duration": metadata["duration"],
    }


REDDIT_URL_PATTERN = re.compile(r"reddit\.com/r/([^/]+)/comments/[^/]+/([^/]+)")


def scrape_reddit_rss(url: str):
    """
    Reddit's own RSS/Atom feed for a single post. Confirmed working (200,
    real content) even when .json and generic scraping are both blocked
    with a 403 or interstitial page.
    """
    rss_url = url.split("?")[0].rstrip("/") + ".rss"
    reddit_headers = {"User-Agent": "emailme-script/1.0"}
    try:
        resp = requests.get(rss_url, headers=reddit_headers, timeout=10)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "xml")
        entry = soup.find("entry")
        if not entry:
            return None

        title_tag = entry.find("title")
        title = str(title_tag.string) if title_tag and title_tag.string else None
        if not title:
            return None

        content_tag = entry.find("content")
        description = None
        image_url = None
        if content_tag and content_tag.string:
            content_html = str(content_tag.string)
            content_soup = BeautifulSoup(content_html, "html.parser")
            img_tag = content_soup.find("img")
            if img_tag and img_tag.get("src"):
                image_url = img_tag["src"].replace("&amp;", "&")
            text = content_soup.get_text(separator=" ", strip=True)
            description = text[:300] if text else None

        return {
            "final_url": url,
            "title": title,
            "description": description,
            "image_url": image_url,
        }
    except Exception as e:
        log.warning(f"Reddit RSS failed: {e}")
        return None


def scrape_reddit_json(url: str):
    """
    Try Reddit's JSON endpoint for title, description, and image.
    Falls back to URL slug parsing if JSON fails.
    """

    match = REDDIT_URL_PATTERN.search(url)
    if not match:
        return None

    subreddit, slug = match.groups()

    post_id_match = re.search(r"/comments/([a-z0-9]+)/", url)
    if not post_id_match:
        return None

    post_id = post_id_match.group(1)

    json_url = f"https://www.reddit.com/comments/{post_id}/.json"

    reddit_headers = {"User-Agent": "emailme-script/1.0"}

    try:
        r = requests.get(json_url, headers=reddit_headers, timeout=10)

        if r.status_code == 200:
            data = r.json()

            post = data[0]["data"]["children"][0]["data"]

            image = None

            if post.get("preview"):
                image = (
                    post["preview"].get("images", [{}])[0].get("source", {}).get("url")
                )

            return {
                "final_url": url,
                "title": post.get("title"),
                "description": post.get("selftext"),
                "image_url": image.replace("&amp;", "&") if image else None,
            }

    except Exception as e:
        log.warning(f"Reddit JSON failed: {e}")

    # fallback
    title = slug.replace("_", " ").strip().lower()

    return {
        "final_url": url,
        "title": f"{title} (r/{subreddit})",
        "description": None,
        "image_url": None,
    }


def synthesize_note_for_orphan_image(image_path: Path):
    """
    Create a minimal .md note embedding the orphan image, so it flows
    through the normal note pipeline instead of needing a separate one.
    """
    note_path = image_path.with_suffix(".md")
    counter = 1
    while note_path.exists():
        note_path = image_path.parent / f"{image_path.stem}_{counter}.md"
        counter += 1
    note_path.write_text(f"![[{image_path.name}]]", encoding="utf-8")
    return note_path


# ---------------------------------------------------------------------------
# STEP 4 - build and send the email
# ---------------------------------------------------------------------------


def get_source_label(url):
    try:
        host = urlparse(url).netloc
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared email styling - same S_* constants and values as fed_digest.py
# (output style guide, Category A). All styles inline (Gmail strips
# <head><style> in some rendering contexts).
# ---------------------------------------------------------------------------

S_BODY = "font-family:'Atkinson Hyperlegible',-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#123524;color:#F0EBE0;max-width:680px;margin:0 auto;padding:24px 16px;"
S_TAGBOX = "border:1px solid #2F5B3E;border-radius:8px;padding:12px 16px;margin:0;background:#1A4A30;"
S_TAGNAME = "font-family:'Bricolage Grotesque',-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-weight:bold;font-size:15px;color:#FF8C42;"
S_META = "color:#B8AFA0;font-size:12px;"
S_LINK = "color:#FF8C42;text-decoration:none;"
BYLINE_SEP = " \u00b7 "  # middle dot; kept as a plain constant (not inline in an
# f-string) since Python < 3.12 disallows a backslash escape inside an f-string's {} part


def _a(url: str, inner_html: str) -> str:
    """A single styled <a> tag - link color, no underline - used everywhere a link appears in the email body."""
    return f"<a href='{url}' style='{S_LINK}'>{inner_html}</a>"


def format_captured_date(note_path: Path) -> str:
    """Human-readable capture date, no time (e.g. 'Aug 7, 2026'), from the note's own capture timestamp."""
    dt = extract_timestamp(note_path)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def build_email_subject(classification: dict, link_data_list: list) -> str:
    """
    Pick a subject line based on what kind of capture this is: YouTube
    channel + title, Reddit subreddit + title, plain link title, first
    line of typed text, or a generic label for a bare image. Whitespace
    is collapsed at the end since raw page titles (soup.title.string, for
    instance) can carry embedded newlines that would otherwise crash the
    email header.
    """
    if (
        classification["tag"] == "YT"
        and link_data_list
        and link_data_list[0].get("title")
    ):
        channel = link_data_list[0].get("channel")
        title = link_data_list[0]["title"]
        subject = f"{channel} - {title}" if channel else title
    elif link_data_list and link_data_list[0].get("title"):
        link = link_data_list[0]
        if link.get("reddit_subreddit"):
            subject = f"{link['reddit_subreddit']} {link['title']}"
        else:
            subject = link["title"]
    elif classification["body_text"]:
        subject = classification["body_text"].splitlines()[0][:80]
    else:
        subject = "Image capture"

    return " ".join(f"[{classification['tag']}] {subject} #emailme".split())


def render_youtube_card(link: dict, captured_date: str) -> list:
    """HTML parts for a single YouTube link in the links-only email branch."""
    parts = [
        f"<p style='{S_TAGNAME}'>{_a(link['final_url'], html_module.escape(link.get('title') or link['final_url']))}</p>"
    ]
    byline = [captured_date]
    if link.get("channel"):
        byline.insert(0, link["channel"])
    parts.append(f"<p style='{S_META}'>{html_module.escape(BYLINE_SEP.join(byline))}</p>")

    yt_facts = [
        f for f in (link.get("view_count"), link.get("upload_date"), link.get("duration")) if f
    ]
    if yt_facts:
        parts.append(f"<p style='{S_META}'>{html_module.escape(BYLINE_SEP.join(yt_facts))}</p>")

    if link.get("image_url"):
        img_tag = f"<img src='{link['image_url']}' style='max-width:400px;'>"
        parts.append(_a(link["final_url"], img_tag))
    if link.get("description"):
        description_html = html_module.escape(link["description"]).replace("\n", "<br>")
        parts.append(f"<p>{description_html}</p>")
    parts.append(f"<p>{_a(link['final_url'], link['final_url'])}</p>")
    return parts


def render_reddit_card(link: dict, captured_date: str) -> list:
    """HTML parts for a single Reddit link (with author+subreddit) in the links-only email branch."""
    if link.get("reddit_method"):
        log.info(f"Reddit method used for email: {link['reddit_method']}")

    parts = [
        f"<p style='{S_TAGNAME}'>{_a(link['final_url'], html_module.escape(link.get('title') or link['final_url']))}</p>"
    ]

    author_url = f"https://www.reddit.com/{link['reddit_author']}"
    subreddit_url = f"https://www.reddit.com/{link['reddit_subreddit']}"
    byline_html = (
        f"submitted by {_a(author_url, html_module.escape(link['reddit_author']))} "
        f"to {_a(subreddit_url, html_module.escape(link['reddit_subreddit']))} \u00b7 {captured_date}"
    )
    parts.append(f"<p style='{S_META}'>{byline_html}</p>")

    actions = []
    if link.get("reddit_link_url"):
        actions.append(_a(link["reddit_link_url"], "[link]"))
    if link.get("reddit_comments_url"):
        actions.append(_a(link["reddit_comments_url"], "[comments]"))
    if actions:
        parts.append(f"<p style='{S_META}'>{', '.join(actions)}</p>")

    if link.get("image_url"):
        img_tag = f"<img src='{link['image_url']}' style='max-width:400px;'>"
        parts.append(_a(link["final_url"], img_tag))
    parts.append(f"<p>{_a(link['final_url'], link['final_url'])}</p>")
    return parts


def render_generic_link_card(link: dict, captured_date: str) -> list:
    """HTML parts for a single non-YouTube, non-Reddit link in the links-only email branch."""
    if link.get("reddit_method"):
        log.info(f"Reddit method used for email: {link['reddit_method']}")

    parts = [
        f"<p style='{S_TAGNAME}'>{_a(link['final_url'], html_module.escape(link.get('title') or link['final_url']))}</p>"
    ]
    byline = []
    source = get_source_label(link.get("final_url", ""))
    if source:
        byline.append(source)
    if link.get("published"):
        byline.append(link["published"])
    byline.append(captured_date)
    parts.append(f"<p style='{S_META}'>{html_module.escape(BYLINE_SEP.join(byline))}</p>")

    if link.get("description"):
        parts.append(f"<p>{html_module.escape(link['description'])}</p>")
    if link.get("image_url"):
        img_tag = f"<img src='{link['image_url']}' style='max-width:400px;'>"
        parts.append(_a(link["final_url"], img_tag))
    parts.append(f"<p>{_a(link['final_url'], link['final_url'])}</p>")
    return parts


def build_and_send_email(
    classification: dict, link_data_list: list, image_path, note_path: Path
):
    """
    Assemble subject + HTML body from whatever combination of text/links/image
    is present, then send via Gmail SMTP.
    Raises an exception on failure - caller catches it and marks the note _FAILED.
    Per-link-type HTML rendering for the links-only branch lives in
    render_youtube_card/render_reddit_card/render_generic_link_card; subject
    logic lives in build_email_subject.
    """
    msg = EmailMessage()
    msg["Subject"] = build_email_subject(classification, link_data_list)
    msg["From"] = formataddr(("#emailme", GMAIL_ADDRESS))
    msg["To"] = EMAIL_TO

    captured_date = format_captured_date(note_path)
    html_parts = []

    if classification["urls"] and classification["has_extra_text"]:
        text_with_links = classification["body_text"]
        for url, link in zip(classification["urls"], link_data_list):
            link_text = link.get("title") or link["final_url"]
            anchor = _a(link["final_url"], html_module.escape(link_text))
            text_with_links = text_with_links.replace(url, anchor)
        text_with_links = text_with_links.replace("\n", "<br>")
        html_parts.append(f"<p>{text_with_links}</p>")

        for link in link_data_list:
            byline = []
            if classification["tag"] == "YT" and link.get("channel"):
                byline.append(link["channel"])
            elif not link.get("reddit_subreddit"):
                source = get_source_label(link.get("final_url", ""))
                if source:
                    byline.append(source)
            byline.append(captured_date)
            html_parts.append(
                f"<p style='{S_META}'>{html_module.escape(BYLINE_SEP.join(byline))}</p>"
            )
            if link.get("published"):
                html_parts.append(
                    f"<p style='{S_META}'>{html_module.escape(link['published'])}</p>"
                )
            if classification["tag"] == "YT":
                yt_facts = [
                    f
                    for f in (link.get("view_count"), link.get("upload_date"), link.get("duration"))
                    if f
                ]
                if yt_facts:
                    html_parts.append(
                        f"<p style='{S_META}'>{html_module.escape(BYLINE_SEP.join(yt_facts))}</p>"
                    )
            if link.get("description"):
                html_parts.append(f"<p>{html_module.escape(link['description'])}</p>")
            if link.get("image_url"):
                img_tag = f"<img src='{link['image_url']}' style='max-width:400px;'>"
                html_parts.append(_a(link["final_url"], img_tag))

    elif classification["body_text"] and not link_data_list:
        text_html = html_module.escape(classification["body_text"]).replace(
            "\n", "<br>"
        )
        html_parts.append(f"<p>{text_html}</p>")
        html_parts.append(f"<p style='{S_META}'>{captured_date}</p>")

    else:
        for link in link_data_list:
            if classification["tag"] == "YT":
                html_parts.extend(render_youtube_card(link, captured_date))
            elif link.get("reddit_author") and link.get("reddit_subreddit"):
                html_parts.extend(render_reddit_card(link, captured_date))
            else:
                html_parts.extend(render_generic_link_card(link, captured_date))

    if image_path:
        html_parts.append(f"<p style='{S_META}'>(image attached)</p>")

    msg.set_content("This email requires HTML to view properly.")
    card = f"<div style='{S_TAGBOX}'>{''.join(html_parts)}</div>"
    full_html = f"<html><body style=\"margin:0;padding:0;background:#123524;\"><div style=\"{S_BODY}\">{card}</div></body></html>"
    msg.add_alternative(full_html, subtype="html")

    if image_path:
        image_bytes = image_path.read_bytes()
        subtype = image_path.suffix.lstrip(".").lower() or "png"
        msg.add_attachment(
            image_bytes, maintype="image", subtype=subtype, filename=image_path.name
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# STEP 5 - rename and move
# ---------------------------------------------------------------------------


def unique_path(folder: Path, name: str, ext: str) -> Path:
    """
    Return folder/name+ext, or folder/name_2+ext, folder/name_3+ext, etc.
    if that name is already taken. Prevents same-second renames from
    colliding and crashing on Path.rename().
    """
    candidate = folder / f"{name}{ext}"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{name}_{counter}{ext}"
        counter += 1
    return candidate


def finalize_note(
    note_path: Path, tag: str, failed: bool, image_path: Path = None, error: str = None
):
    """
    Rename per convention and move to archive/ on success. On failure,
    leave it in root with an incremented _FAILED.<n>. prefix so the next
    run retries it, preserving the note's original capture timestamp (not
    the failure time) so retries of the same note stay linked together.
    After MAX_RETRY_ATTEMPTS failures, rename it with a permanent
    _FAILED.IGNORED. marker instead (see IGNORED_PATTERN) and send a
    one-time notification email, since find_unprocessed_notes() will never
    pick it up again automatically past that point.
    """
    if failed:
        original_timestamp = extract_timestamp(note_path).strftime("%Y.%m.%d_%H.%M.%S")
        attempt = get_retry_attempt(note_path) + 1
        if attempt >= MAX_RETRY_ATTEMPTS:
            dest = unique_path(VAULT_ROOT, f"_FAILED.IGNORED.{original_timestamp}", f"_{tag}.md")
            note_path.rename(dest)
            try:
                send_give_up_notification(dest, tag, error)
            except Exception as notify_error:
                log.error(f"Also failed to send give-up notification: {notify_error}")
        else:
            dest = unique_path(VAULT_ROOT, f"_FAILED.{attempt}.{original_timestamp}", f"_{tag}.md")
            note_path.rename(dest)
        # image is left where it is on failure, so it can be retried
    else:
        timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
        ARCHIVE_DIR.mkdir(exist_ok=True)
        dest = unique_path(ARCHIVE_DIR, timestamp, f"_{tag}.md")
        note_path.rename(dest)

        if image_path and image_path.exists():
            old_image_name = image_path.name
            img_dest = unique_path(
                ARCHIVE_DIR, f"{timestamp}_{tag}", image_path.suffix.lower()
            )
            image_path.rename(img_dest)

            # the embed in the note text still points at the image's old
            # filename; rewrite it to match the new archived name so the
            # link doesn't break once both files are moved. find_embedded_image()
            # accepts both Obsidian wikilink (![[name]]) and markdown (![alt](name))
            # embed syntax, so both need rewriting here too, not just wikilink.
            note_text = dest.read_text(encoding="utf-8")
            updated_text = note_text.replace(
                f"![[{old_image_name}]]", f"![[{img_dest.name}]]"
            )
            markdown_embed = re.compile(
                r"(!\[.*?\]\()" + re.escape(old_image_name) + r"(\))"
            )
            updated_text = markdown_embed.sub(
                rf"\g<1>{img_dest.name}\g<2>", updated_text
            )
            dest.write_text(updated_text, encoding="utf-8")


def send_give_up_notification(note_path: Path, tag: str, error: str):
    """
    Sent once, when a failed note has exhausted MAX_RETRY_ATTEMPTS and
    won't be retried automatically anymore. Lets Klif know it needs manual
    review instead of silently sitting in root forever with no signal.
    """
    msg = EmailMessage()
    msg["Subject"] = " ".join(f"[{tag}] Failed {MAX_RETRY_ATTEMPTS}x, giving up #emailme".split())
    msg["From"] = formataddr(("#emailme", GMAIL_ADDRESS))
    msg["To"] = EMAIL_TO
    body = (
        f"This note failed on {MAX_RETRY_ATTEMPTS} separate runs and won't be retried "
        f"automatically anymore.\n\n"
        f"File: {note_path.name}\n"
        f"Last error: {error}\n\n"
        f"To retry it, rename it to drop the _FAILED.IGNORED. prefix "
        f"(or fix whatever's wrong first) and leave it in {VAULT_ROOT}."
    )
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def process_note(note_path: Path):
    """Runs one capture note through the full pipeline: classify, scrape any links, email it, then archive it."""
    text = note_path.read_text(encoding="utf-8")
    image_path = find_embedded_image(note_path)
    classification = classify_note(text, has_image=image_path is not None)

    link_data_list = []
    for url in classification["urls"]:
        try:
            link_data_list.append(resolve_and_scrape(url))
        except Exception as e:
            log.warning(f"Link failed, continuing with a bare link: {url} ({e})")
            link_data_list.append(
                {
                    "final_url": url,
                    "title": None,
                    "description": None,
                    "image_url": None,
                }
            )

    if classification["tag"] != "IMG" and any(
        YOUTUBE_PATTERN.search(link.get("final_url", "")) for link in link_data_list
    ):
        classification["tag"] = "YT"
    
    build_and_send_email(classification, link_data_list, image_path, note_path)
    finalize_note(note_path, classification["tag"], failed=False, image_path=image_path)


PURGE_AFTER_DAYS = 8
PURGE_MARKER_FILE = VAULT_ROOT / ".last_purge"


def purge_old_archives():
    """
    Runs at most once a week. Deletes anything in archive/ older than
    PURGE_AFTER_DAYS, based on the file's own last-modified time.
    _FAILED. notes are never auto-deleted, since those need manual review.
    """
    now = datetime.now()

    if PURGE_MARKER_FILE.exists():
        last_purge = datetime.fromtimestamp(PURGE_MARKER_FILE.stat().st_mtime)
        if (now - last_purge).days < 7:
            return

    cutoff = now - timedelta(days=PURGE_AFTER_DAYS)
    deleted_count = 0

    if ARCHIVE_DIR.exists():
        for path in ARCHIVE_DIR.iterdir():
            if path.is_file():
                modified = datetime.fromtimestamp(path.stat().st_mtime)
                if modified < cutoff:
                    path.unlink()
                    deleted_count += 1

    log.info(f"Purge: deleted {deleted_count} file(s) older than {PURGE_AFTER_DAYS} days.")
    PURGE_MARKER_FILE.write_text(now.isoformat(), encoding="utf-8")


LOCK_FILE = VAULT_ROOT / ".watcher.lock"


def run_watcher():
    """Main entry point: claims the lock, processes every unprocessed note and orphan image, then purges old archives."""
    if LOCK_FILE.exists():
        lock_age = datetime.now() - datetime.fromtimestamp(LOCK_FILE.stat().st_mtime)
        if lock_age.total_seconds() < 3600:
            log.info(
                "Another run appears to be in progress (lock file present and recent). Skipping this run."
            )
            return
        else:
            log.warning(
                "Stale lock file found (older than an hour); removing it and continuing."
            )

    LOCK_FILE.write_text(datetime.now().isoformat(), encoding="utf-8")
    try:
        log.info("Scanning for new captures...")

        notes = find_unprocessed_notes()

        claimed_images = set()
        for note_path in notes:
            img = find_embedded_image(note_path)
            if img:
                claimed_images.add(img.resolve())

        for image_path in find_orphan_images(claimed_images):
            notes.append(synthesize_note_for_orphan_image(image_path))
        notes.sort(key=extract_timestamp)

        log.info(f"Found {len(notes)} item(s) to process.")

        for note_path in notes:
            log.info(f"Processing: {note_path.name}")
            try:
                process_note(note_path)
                log.info("Done: sent and archived.")
            except Exception as e:
                log.error(f"FAILED: {e}")
                traceback.print_exc()
                try:
                    text = note_path.read_text(encoding="utf-8")
                    has_image = find_embedded_image(note_path) is not None
                    classification = classify_note(text, has_image)
                    finalize_note(note_path, classification["tag"], failed=True, error=str(e))
                except Exception as recovery_error:
                    log.error(f"ALSO FAILED to mark as failed: {recovery_error}")
                    traceback.print_exc()

        purge_old_archives()
        log.info("All items processed.")
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def wait_before_exit(timeout=5):
    """
    Manual-run convenience only. If there's no console attached (e.g. run
    hidden/logged-off by Task Scheduler), this quietly does nothing instead
    of throwing.
    """
    try:
        if not sys.stdout.isatty():
            return
        print(
            f"\nDone. Press any key within {timeout} seconds to keep this window open..."
        )
        start = time.time()
        while time.time() - start < timeout:
            if msvcrt.kbhit():
                msvcrt.getch()
                print("Staying open. Press Enter to close.")
                input()
                return
            time.sleep(0.1)
        print("No key pressed, closing...")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        run_watcher()
    except Exception:
        log.error("Unexpected error:")
        traceback.print_exc()
    wait_before_exit()