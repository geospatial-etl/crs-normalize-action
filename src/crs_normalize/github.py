"""Posting the report back to a pull request via the GitHub REST API.

Every failure mode here is non-fatal. A missing token, a fork PR whose token
has read-only permissions, or an API outage must not turn a passing CRS check
into a failing build, so all errors are logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

__all__ = ["PullRequestCommenter", "detect_pull_request_number", "upsert_comment"]

logger = logging.getLogger(__name__)

#: Hidden HTML marker used to find a comment this tool previously posted, so
#: repeated runs update one comment instead of appending a new one each push.
COMMENT_MARKER = "<!-- crs-normalize-action -->"

_API_VERSION = "2022-11-28"
_TIMEOUT = 20.0


def detect_pull_request_number(event_path: str | None = None) -> int | None:
    """Determine the pull request number from the GitHub event payload.

    Args:
        event_path: Path to the event JSON. Defaults to ``GITHUB_EVENT_PATH``.

    Returns:
        The pull request number, or ``None`` when the workflow was not
        triggered by a pull request.
    """
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not read GitHub event payload: %s", exc)
        return None

    for key in ("pull_request", "issue"):
        node = payload.get(key)
        if isinstance(node, dict) and isinstance(node.get("number"), int):
            return int(node["number"])
    number = payload.get("number")
    return int(number) if isinstance(number, int) else None


class PullRequestCommenter:
    """Creates or updates a single sticky comment on a pull request.

    Args:
        token: A GitHub token with ``pull-requests: write``.
        repository: ``owner/name`` slug. Defaults to ``GITHUB_REPOSITORY``.
        api_url: API base URL. Defaults to ``GITHUB_API_URL`` or github.com.
        client: Pre-built HTTP client, primarily for testing.
    """

    def __init__(
        self,
        token: str,
        repository: str | None = None,
        api_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        """Return the authentication and content-negotiation headers."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _open(self) -> httpx.Client:
        """Return the HTTP client to use, creating one when none was injected."""
        return self._client or httpx.Client(timeout=_TIMEOUT)

    def upsert(self, pr_number: int, body: str) -> bool:
        """Create the report comment, or update the one already posted.

        Args:
            pr_number: Pull request number.
            body: Markdown body. The sticky marker is prepended automatically.

        Returns:
            ``True`` when the comment was written, ``False`` when it was not
            (for any reason, all of which are logged rather than raised).
        """
        if not self.repository:
            logger.warning("No repository slug available; skipping pull request comment.")
            return False

        payload_body = f"{COMMENT_MARKER}\n{body}"
        base = f"{self.api_url}/repos/{self.repository}"
        client = self._open()
        own_client = self._client is None

        try:
            existing_id = self._find_existing(client, base, pr_number)
            if existing_id is not None:
                response = client.patch(
                    f"{base}/issues/comments/{existing_id}",
                    headers=self._headers,
                    json={"body": payload_body},
                )
            else:
                response = client.post(
                    f"{base}/issues/{pr_number}/comments",
                    headers=self._headers,
                    json={"body": payload_body},
                )
            if response.status_code >= 400:
                logger.warning(
                    "Could not post pull request comment (HTTP %s). This does not affect the "
                    "CRS check result; the token most likely lacks 'pull-requests: write'.",
                    response.status_code,
                )
                return False
            return True
        except httpx.HTTPError as exc:
            logger.warning("Could not reach the GitHub API to post a comment: %s", exc)
            return False
        finally:
            if own_client:
                client.close()

    def _find_existing(self, client: httpx.Client, base: str, pr_number: int) -> int | None:
        """Return the id of a previously posted comment, if one exists."""
        try:
            response = client.get(
                f"{base}/issues/{pr_number}/comments",
                headers=self._headers,
                params={"per_page": 100},
            )
        except httpx.HTTPError as exc:
            logger.debug("Listing comments failed: %s", exc)
            return None
        if response.status_code >= 400:
            logger.debug("Listing comments returned HTTP %s", response.status_code)
            return None
        try:
            comments = response.json()
        except ValueError:
            return None
        if not isinstance(comments, list):
            return None
        for comment in reversed(comments):
            if isinstance(comment, dict) and COMMENT_MARKER in str(comment.get("body", "")):
                identifier = comment.get("id")
                if isinstance(identifier, int):
                    return identifier
        return None


def upsert_comment(body: str, token: str | None = None) -> bool:
    """Post ``body`` to the current pull request, if there is one.

    A convenience wrapper that pulls the token, repository and pull request
    number out of the standard GitHub Actions environment and degrades quietly
    when any of them is absent.

    Args:
        body: Markdown body to post.
        token: Token to authenticate with. Defaults to ``GITHUB_TOKEN``.

    Returns:
        ``True`` when a comment was created or updated.
    """
    resolved = token or os.environ.get("GITHUB_TOKEN")
    if not resolved:
        logger.info("No GITHUB_TOKEN available; skipping pull request comment.")
        return False

    pr_number = detect_pull_request_number()
    if pr_number is None:
        logger.info("Not running on a pull request; skipping pull request comment.")
        return False

    return PullRequestCommenter(resolved).upsert(pr_number, body)
