import re
import json
import logging
import shutil
import smtplib
import sys
import traceback
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
import html as html_module
import msvcrt
import time

import warnings
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

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

from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD, VAULT_ROOT as VAULT_ROOT_STR
VAULT_ROOT = Path(VAULT_ROOT_STR)
ARCHIVE_DIR = VAULT_ROOT / "archive"

EMAIL_TO = GMAIL_ADDRESS                    # sending to yourself; change if different

IMAGE_MODE = "attachment"  # "attachment" or "inline" - undecided per your notes, pick later

# Normal browser User-Agent, so sites don't serve interstitial/verification
# pages to an obvious script (this was causing the Reddit/aweber title bug).
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# Matches filenames the watcher has already produced, success or failure.
# If a file matches this, skip it - it's already handled.
PROCESSED_PATTERN = re.compile(
    r"^(_FAILED\.)?\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(_\d+)?_(TXT|URL|YT|IMG)\.md$"
)

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
    that don't match PROCESSED_PATTERN - these are new captures. Sorted by
    actual capture timestamp, so a batch of shares gets emailed and archived
    in the order they were originally shared, not filesystem/alphabetical
    order.
    """
    notes = []
    for path in VAULT_ROOT.glob("*.md"):
        if not PROCESSED_PATTERN.match(path.name):
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
    feed_tag = page_soup.find("link", rel="alternate", type=re.compile(r"(rss|atom)\+xml"))
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
            entry_link = link_tag.get("href") or (str(link_tag.string) if link_tag.string else None)

        if entry_link and entry_link.rstrip("/") != page_url.rstrip("/"):
            return None

        title_tag = entry.find("title")
        title = str(title_tag.string) if title_tag and title_tag.string else None
        if not title:
            return None

        description = None
        desc_tag = entry.find("description") or entry.find("summary") or entry.find("content")
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
        print("General RSS fallback failed:", e)
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
                print(f"Got {resp.status_code}, retrying in {delay}s (attempt {attempt}/{max_attempts})...")
                time.sleep(delay)
                delay *= 2
                continue
        return resp
    return resp

def resolve_and_scrape(url: str):
    """
    Follow redirects to the final URL. If it's a YouTube link, use oEmbed.
    Otherwise scrape OG title/description/image via BeautifulSoup.
    Returns a dict: {final_url, title, description, image_url} - any field
    may be None if not found.
    """
    if "reddit.com" in url:
        resp = fetch_with_backoff(url, headers=HEADERS, timeout=10)
    else:
        resp = requests.get(url, allow_redirects=True, timeout=10, headers=HEADERS)

    final_url = resp.url

    if "youtube.com" in final_url or "youtu.be" in final_url:
        yt_data = scrape_youtube_oembed(final_url)
        if yt_data.get("title"):
            return yt_data

    if "reddit.com" in final_url:
        # Layer 1: real OG scraping, richest result when Reddit isn't
        # currently blocking the request
        soup = BeautifulSoup(resp.text, "html.parser")

        def og(prop):
            tag = soup.find("meta", property=f"og:{prop}")
            return tag["content"] if tag and tag.get("content") else None

        og_title = og("title")
        if og_title and "please wait" not in og_title.lower() and "just a moment" not in og_title.lower():
            raw_description = og("description")
            author = None
            if raw_description:
                submitted_match = re.search(r"submitted by /?(u/\S+?)(?:\s*\[link\]|\s*to\s|\s*$)", raw_description)
                if submitted_match:
                    author = submitted_match.group(1)

            subreddit_from_desc = None
            subreddit_match = REDDIT_URL_PATTERN.search(final_url)
            if subreddit_match:
                subreddit_from_desc = f"r/{subreddit_match.group(1)}"

            print(f"Reddit raw description: {raw_description!r}")
            print(f"Reddit parsed author={author!r} subreddit={subreddit_from_desc!r}")

            comments_url = final_url
            og_url_tag = og("url")
            link_url = og_url_tag if og_url_tag and og_url_tag != final_url else None

            print("Reddit method used: OG scrape")

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
            print("Reddit method used: RSS")
            rss_data["reddit_method"] = "RSS"
            return rss_data

        # Layer 3: Reddit's JSON endpoint, kept as a real attempt so it
        # self-heals automatically if Reddit's block ever lifts
        json_data = scrape_reddit_json(final_url)
        if json_data and json_data.get("title") and not json_data["title"].endswith(")"):
            print("Reddit method used: JSON")
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
                print("Reddit method used: slug parsing")
                return {
                    "final_url": final_url,
                    "title": f"{title} (r/{subreddit})",
                    "description": None,
                    "image_url": None,
                    "reddit_method": "slug parsing",
                }
        print("Reddit method used: all methods failed")
        return {"final_url": final_url, "title": None, "description": None, "image_url": None, "reddit_method": "all methods failed"}

    soup = BeautifulSoup(resp.text, "html.parser")
    def og(prop):
        tag = soup.find("meta", property=f"og:{prop}")
        return tag["content"] if tag and tag.get("content") else None

    og_title = og("title") or (str(soup.title.string) if soup.title and soup.title.string else None)
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

    return {"final_url": final_url, "title": None, "description": None, "image_url": None}


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
        contents = (
            data["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"]
        )
        for item in contents:
            video_secondary_info = item.get("videoSecondaryInfoRenderer")
            if video_secondary_info:
                description_obj = video_secondary_info.get("attributedDescription") or video_secondary_info.get("description")
                if description_obj and description_obj.get("content"):
                    full_text = description_obj["content"]
                    print(f"YouTube full description extracted: {len(full_text)} chars")
                    return full_text
                if description_obj and description_obj.get("runs"):
                    full_text = "".join(run.get("text", "") for run in description_obj["runs"])
                    print(f"YouTube full description extracted: {len(full_text)} chars")
                    return full_text
    except (KeyError, IndexError, TypeError):
        pass
    print("YouTube full description extraction found nothing, will fall back to short og:description")
    return None


def scrape_youtube_oembed(url: str):
    """
    YouTube's oEmbed endpoint gives title, thumbnail, and channel name, but
    has no description field at all. To get a real description, also fetch
    the video page directly and pull its og:description tag.
    """
    api = f"https://www.youtube.com/oembed?url={url}&format=json"
    resp = requests.get(api, timeout=10, headers=HEADERS)

    if resp.status_code != 200:        return {"final_url": url, "title": None, "description": None, "image_url": None, "channel": None}
    data = resp.json()

    description = None
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
    except Exception:
        pass  # description is a nice-to-have; don't fail the whole capture over it

    return {
        "final_url": url,
        "title": data.get("title"),
        "description": description,
        "image_url": data.get("thumbnail_url"),
        "channel": data.get("author_name"),
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
        print("Reddit RSS failed:", e)
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

    reddit_headers = {
        "User-Agent": "emailme-script/1.0"
    }

    try:
        r = requests.get(
            json_url,
            headers=reddit_headers,
            timeout=10
        )

        

        if r.status_code == 200:
            data = r.json()

            post = data[0]["data"]["children"][0]["data"]

            image = None

            if post.get("preview"):
                image = (
                    post["preview"]
                    .get("images", [{}])[0]
                    .get("source", {})
                    .get("url")
                )

            return {
                "final_url": url,
                "title": post.get("title"),
                "description": post.get("selftext"),
                "image_url": image.replace("&amp;", "&") if image else None,
            }

    except Exception as e:
        print("Reddit JSON failed:", e)

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

def build_and_send_email(classification: dict, link_data_list: list, image_path):
    """
    Assemble subject + HTML body from whatever combination of text/links/image
    is present, then send via Gmail SMTP.
    Raises an exception on failure - caller catches it and marks the note _FAILED.
    """
    msg = EmailMessage()

    if classification["tag"] == "YT" and link_data_list and link_data_list[0].get("title"):
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

    msg["Subject"] = f"[{classification['tag']}] {subject} #emailme"
    msg["From"] = formataddr(("#emailme", GMAIL_ADDRESS))
    msg["To"] = EMAIL_TO

    TITLE_STYLE = "font-size:1.2em;font-weight:bold;"
    META_STYLE = "font-style:italic;color:#666;font-size:0.9em;"

    html_parts = []

    if classification["urls"] and classification["has_extra_text"]:
        text_with_links = classification["body_text"]
        for url, link in zip(classification["urls"], link_data_list):
            link_text = link.get("title") or link["final_url"]
            anchor = f"<a href='{link['final_url']}'>{html_module.escape(link_text)}</a>"
            text_with_links = text_with_links.replace(url, anchor)
        text_with_links = text_with_links.replace("\n", "<br>")
        html_parts.append(f"<p>{text_with_links}</p>")

        for link in link_data_list:
            if classification["tag"] == "YT" and link.get("channel"):
                html_parts.append(f"<p style='{META_STYLE}'>{html_module.escape(link['channel'])}</p>")
            elif not link.get("reddit_subreddit"):
                source = get_source_label(link.get("final_url", ""))
                if source:
                    html_parts.append(f"<p style='{META_STYLE}'>Source: {html_module.escape(source)}</p>")
            if link.get("published"):
                html_parts.append(f"<p style='{META_STYLE}'>{html_module.escape(link['published'])}</p>")
            if link.get("description"):
                html_parts.append(f"<p>{html_module.escape(link['description'])}</p>")
            if link.get("image_url"):
                html_parts.append(
                    f"<a href='{link['final_url']}'><img src='{link['image_url']}' style='max-width:400px;'></a>"
                )

    elif classification["body_text"] and not link_data_list:
        text_html = html_module.escape(classification["body_text"]).replace("\n", "<br>")
        html_parts.append(f"<p>{text_html}</p>")

    else:
        for link in link_data_list:
            if classification["tag"] == "YT":
                html_parts.append(f"<p style='{TITLE_STYLE}'><a href='{link['final_url']}'>{html_module.escape(link.get('title') or link['final_url'])}</a></p>")
                if link.get("channel"):
                    html_parts.append(f"<p style='{META_STYLE}'>{html_module.escape(link['channel'])}</p>")
                if link.get("image_url"):
                    html_parts.append(
                        f"<a href='{link['final_url']}'><img src='{link['image_url']}' style='max-width:400px;'></a>"
                    )
                if link.get("description"):
                    description_html = html_module.escape(link["description"]).replace("\n", "<br>")
                    html_parts.append(f"<p>{description_html}</p>")
                html_parts.append(f"<p><a href='{link['final_url']}'>{link['final_url']}</a></p>")
            elif link.get("reddit_author") and link.get("reddit_subreddit"):
                if link.get("reddit_method"):
                    html_parts.append(f"<p style='color:#888;font-size:0.85em;'>[debug: {link['reddit_method']}]</p>")
                html_parts.append(f"<p style='{TITLE_STYLE}'><a href='{link['final_url']}'>{html_module.escape(link.get('title') or link['final_url'])}</a></p>")

                author_url = f"https://www.reddit.com/{link['reddit_author']}"
                subreddit_url = f"https://www.reddit.com/{link['reddit_subreddit']}"
                html_parts.append(
                    f"<p style='{META_STYLE}'>submitted by <a href='{author_url}'>{html_module.escape(link['reddit_author'])}</a> "
                    f"to <a href='{subreddit_url}'>{html_module.escape(link['reddit_subreddit'])}</a></p>"
                )

                actions = []
                if link.get("reddit_link_url"):
                    actions.append(f"<a href='{link['reddit_link_url']}'>[link]</a>")
                if link.get("reddit_comments_url"):
                    actions.append(f"<a href='{link['reddit_comments_url']}'>[comments]</a>")
                if actions:
                    html_parts.append(f"<p style='{META_STYLE}'>{', '.join(actions)}</p>")

                if link.get("image_url"):
                    html_parts.append(
                        f"<a href='{link['final_url']}'><img src='{link['image_url']}' style='max-width:400px;'></a>"
                    )
                html_parts.append(f"<p><a href='{link['final_url']}'>{link['final_url']}</a></p>")
            else:
                if link.get("reddit_method"):
                    html_parts.append(f"<p style='color:#888;font-size:0.85em;'>[debug: {link['reddit_method']}]</p>")
                html_parts.append(f"<p style='{TITLE_STYLE}'><a href='{link['final_url']}'>{html_module.escape(link.get('title') or link['final_url'])}</a></p>")
                source = get_source_label(link.get("final_url", ""))
                if source:
                    html_parts.append(f"<p style='{META_STYLE}'>Source: {html_module.escape(source)}</p>")
                if link.get("published"):
                    html_parts.append(f"<p style='{META_STYLE}'>{html_module.escape(link['published'])}</p>")
                if link.get("description"):
                    html_parts.append(f"<p>{html_module.escape(link['description'])}</p>")
                if link.get("image_url"):
                    html_parts.append(
                        f"<a href='{link['final_url']}'><img src='{link['image_url']}' style='max-width:400px;'></a>"
                    )
                html_parts.append(f"<p><a href='{link['final_url']}'>{link['final_url']}</a></p>")

    if image_path and IMAGE_MODE == "attachment":
        html_parts.append(f"<p style='{META_STYLE}'>(image attached)</p>")
    elif image_path and IMAGE_MODE == "inline":
        html_parts.append(f"<img src='cid:embedded_image' style='max-width:400px;'>")

    msg.set_content("This email requires HTML to view properly.")
    full_html = f"<html><body>{''.join(html_parts)}</body></html>"
    msg.add_alternative(full_html, subtype="html")

    if image_path:
        image_bytes = image_path.read_bytes()
        subtype = image_path.suffix.lstrip(".").lower() or "png"
        if IMAGE_MODE == "attachment":
            msg.add_attachment(image_bytes, maintype="image", subtype=subtype, filename=image_path.name)
        else:
            # NOTE: proper inline embedding needs a bit more MIME structure than
            # this simple version handles. Flagging as a follow-up once IMAGE_MODE
            # is actually decided - attachment mode works as written above.
            msg.add_attachment(image_bytes, maintype="image", subtype=subtype, filename=image_path.name)

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


def finalize_note(note_path: Path, tag: str, failed: bool, image_path: Path = None):
    """
    Rename per convention and move to archive/ on success, or leave in root
    with _FAILED. prefix on failure. If image_path is given, move/rename it
    alongside the note on success too, so it won't be re-picked-up as an
    orphan on the next run.
    """
    timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    if failed:
        dest = unique_path(VAULT_ROOT, f"_FAILED.{timestamp}", f"_{tag}.md")
        note_path.rename(dest)
        # image is left where it is on failure, so it can be retried
    else:
        ARCHIVE_DIR.mkdir(exist_ok=True)
        dest = unique_path(ARCHIVE_DIR, timestamp, f"_{tag}.md")
        note_path.rename(dest)

        if image_path and image_path.exists():
            old_image_name = image_path.name
            img_dest = unique_path(ARCHIVE_DIR, f"{timestamp}_{tag}", image_path.suffix.lower())
            image_path.rename(img_dest)

            # the embed in the note text still points at the image's old
            # filename; rewrite it to match the new archived name so the
            # link doesn't break once both files are moved
            note_text = dest.read_text(encoding="utf-8")
            updated_text = note_text.replace(f"![[{old_image_name}]]", f"![[{img_dest.name}]]")
            dest.write_text(updated_text, encoding="utf-8")



# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def process_note(note_path: Path):
    text = note_path.read_text(encoding="utf-8")
    image_path = find_embedded_image(note_path)
    classification = classify_note(text, has_image=image_path is not None)

    link_data_list = []
    for url in classification["urls"]:
        try:
            link_data_list.append(resolve_and_scrape(url))
        except Exception as e:
            print(f"  Link failed, continuing with a bare link: {url} ({e})")
            link_data_list.append({"final_url": url, "title": None, "description": None, "image_url": None})

    build_and_send_email(classification, link_data_list, image_path)
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


    print(f"Purge: deleted {deleted_count} file(s) older than {PURGE_AFTER_DAYS} days.")
    PURGE_MARKER_FILE.write_text(now.isoformat(), encoding="utf-8")

LOCK_FILE = VAULT_ROOT / ".watcher.lock"


def run_watcher():
    if LOCK_FILE.exists():
        lock_age = datetime.now() - datetime.fromtimestamp(LOCK_FILE.stat().st_mtime)
        if lock_age.total_seconds() < 3600:
            print("Another run appears to be in progress (lock file present and recent). Skipping this run.")
            return
        else:
            print("Stale lock file found (older than an hour); removing it and continuing.")

    LOCK_FILE.write_text(datetime.now().isoformat(), encoding="utf-8")
    try:
        print("Scanning for new captures...")

        notes = find_unprocessed_notes()

        claimed_images = set()
        for note_path in notes:
            img = find_embedded_image(note_path)
            if img:
                claimed_images.add(img.resolve())

        for image_path in find_orphan_images(claimed_images):
            notes.append(synthesize_note_for_orphan_image(image_path))
        notes.sort(key=extract_timestamp)

        print(f"Found {len(notes)} item(s) to process.")

        for note_path in notes:
            print(f"Processing: {note_path.name}")
            try:
                process_note(note_path)
                print("  Done: sent and archived.")
            except Exception as e:
                print(f"  FAILED: {e}")
                traceback.print_exc()
                try:
                    text = note_path.read_text(encoding="utf-8")
                    has_image = find_embedded_image(note_path) is not None
                    classification = classify_note(text, has_image)
                    finalize_note(note_path, classification["tag"], failed=True)
                except Exception as recovery_error:
                    print(f"  ALSO FAILED to mark as failed: {recovery_error}")
                    traceback.print_exc()

        purge_old_archives()
        print("All items processed.")
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
        print(f"\nDone. Press any key within {timeout} seconds to keep this window open...")
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
        print("\nUnexpected error:")
        traceback.print_exc()
    wait_before_exit()