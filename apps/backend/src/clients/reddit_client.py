import logging
from contextlib import asynccontextmanager

import asyncpraw
from asyncpraw.models import Submission
from asyncprawcore.exceptions import (
    Forbidden,
    NotFound,
    Redirect,
    RequestException,
    ResponseException,
)

from src.core.config import settings

logger = logging.getLogger(__name__)

# How many top-level comments to render. More than this is rarely useful for
# regex extraction; submissions with hundreds of comments get truncated.
TOP_COMMENT_LIMIT = 30


class RedditFetchError(Exception):
    """Permanent fetch failure — don't retry. Reason is in `.reason`."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RedditUnreachable(Exception):
    """Reddit's API is currently unreachable (TLS / connection failure on auth).
    Different from a per-URL fetch failure — this means we can't even authenticate."""


@asynccontextmanager
async def reddit_session():
    """Read-only async PRAW client. Use as `async with reddit_session() as r: ...`.

    On entry, primes the OAuth token via a single sequential refresh. This is
    REQUIRED to avoid an asyncpraw concurrency bug: if N coroutines hit a fresh
    Reddit instance in parallel, all of them try to refresh the token at once;
    only one succeeds and the others hang for the full 16s timeout.

    Priming serially before fan-out caches the token so concurrent callers all
    hit the cache.
    """
    if not (settings.reddit_client_id and settings.reddit_client_secret):
        raise RuntimeError(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set. "
            "Add them to .env and recreate the container."
        )
    reddit = asyncpraw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
    )
    try:
        try:
            await reddit._core._authorizer.refresh()
        except Exception as e:
            await reddit.close()
            raise RedditUnreachable(
                f"could not authenticate with Reddit: {type(e).__name__}: {e}"
            ) from e
        yield reddit
    finally:
        await reddit.close()


async def fetch_submission_markdown(reddit: asyncpraw.Reddit, url: str) -> str:
    """Fetch a Reddit submission (or comment URL → submission), render it +
    top comments to markdown.

    Raises RedditFetchError on permanent failures (404, 403, deleted, redirect)
    so the caller can mark the row `fetch_failed` cleanly.
    """
    try:
        submission: Submission = await reddit.submission(url=url)
        # Trigger lazy load
        await submission.load()
    except NotFound:
        raise RedditFetchError("not_found")
    except Forbidden:
        raise RedditFetchError("forbidden")
    except Redirect:
        raise RedditFetchError("redirected")
    except ResponseException as e:
        # Other HTTP errors (e.g. 500) — treat as permanent for now;
        # the caller can re-queue if it wants retry behavior.
        raise RedditFetchError(f"http_{e.response.status}")
    except RequestException as e:
        # Network-level failure (timeout, TLS error, connection refused).
        # Reddit may be intermittently blocking us — don't waste a retry now.
        raise RedditUnreachable(f"per-URL network failure: {type(e).__name__}: {e}") from e
    except Exception as e:
        # Defensive — catch any URL-parsing or attribute errors so a single
        # malformed URL doesn't kill the whole stage.
        raise RedditFetchError(f"unexpected:{type(e).__name__}")

    return _render_markdown(submission, await _top_comments(submission))


async def _top_comments(submission: Submission) -> list:
    try:
        # NB: comment_sort can't be changed after load() — submission is already
        # fetched with default sort. Default ('best') is fine for regex extraction.
        # limit=0 flattens "load more comments" stubs (deletes them — we don't
        # want to make extra API calls for low-value comments).
        await submission.comments.replace_more(limit=0)
        comments = list(submission.comments)
    except Exception as e:
        logger.warning("could not load comments for %s: %s", submission.id, e)
        return []
    return comments[:TOP_COMMENT_LIMIT]


def _safe_author(obj) -> str:
    author = getattr(obj, "author", None)
    return getattr(author, "name", None) or "[deleted]"


def _render_markdown(submission: Submission, comments: list) -> str:
    parts: list[str] = []
    parts.append(f"# {submission.title}\n")
    sub_name = getattr(submission.subreddit, "display_name", "?")
    parts.append(f"**r/{sub_name}** | by u/{_safe_author(submission)}\n")
    if submission.url and submission.url != f"https://www.reddit.com{submission.permalink}":
        parts.append(f"\n**Link:** {submission.url}\n")
    selftext = (submission.selftext or "").strip()
    if selftext:
        parts.append(f"\n{selftext}\n")
    if comments:
        parts.append("\n---\n## Top comments\n")
        for c in comments:
            body = (getattr(c, "body", "") or "").strip()
            if not body:
                continue
            parts.append(f"\n### u/{_safe_author(c)}\n{body}\n")
    return "".join(parts)
