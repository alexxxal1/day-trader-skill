#!/usr/bin/env python3
"""Read-only official X API v2 client. Credentials come only from the environment."""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = "https://api.x.com"
MAX_BYTES = 2 * 1024 * 1024


class XUnavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization or follow a redirect to a social website.
        raise XUnavailable("X API redirect refused")


def _get(path, token):
    req = urllib.request.Request(ORIGIN + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json", "User-Agent": "trader-social/1.0"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise XUnavailable("X API response too large")
            result = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not print response bodies, request headers or exception URLs.
        raise XUnavailable("X API HTTP %d" % exc.code) from None
    except (OSError, ValueError, UnicodeError):
        raise XUnavailable("X API request or JSON unavailable") from None
    if not isinstance(result, dict) or result.get("errors"):
        raise XUnavailable("X API incomplete response")
    return result


def fetch_account(account, max_results=10):
    """One account lookup and one bounded timeline request, no scraping or fallback."""
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        return {"status": "DATA UNAVAILABLE", "reason": "X_BEARER_TOKEN missing", "posts": []}
    username = account.get("account", "")
    if (account.get("platform") != "X" or account.get("enabled") is not True
            or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username)
            or type(max_results) is not int or not 5 <= max_results <= 100):
        return {"status": "DATA UNAVAILABLE", "reason": "Invalid X account or limit", "posts": []}
    try:
        user = _get("/2/users/by/username/" + username, token).get("data", {})
        uid = user.get("id", "")
        if (not re.fullmatch(r"[0-9]+", uid)
                or user.get("username", "").lower() != username.lower()
                or (account.get("user_id") and account["user_id"] != uid)):
            raise XUnavailable("X account identity mismatch")
        query = urllib.parse.urlencode({
            "max_results": max_results,
            "tweet.fields": "created_at,author_id,referenced_tweets,note_tweet"})
        path = "/2/users/%s/tweets?%s" % (uid, query)
        doc = _get(path, token)
        rows = doc.get("data", [])
        if not isinstance(rows, list) or ("data" not in doc and doc.get("meta", {}).get("result_count") != 0):
            raise XUnavailable("X API invalid timeline")
        posts = []
        for row in rows:
            if (not isinstance(row, dict) or row.get("author_id") != uid
                    or not re.fullmatch(r"[0-9]+", row.get("id", ""))
                    or not row.get("created_at") or not isinstance(row.get("text"), str)):
                raise XUnavailable("X API missing post provenance")
            refs = row.get("referenced_tweets", [])
            types = {r.get("type") for r in refs}
            kind = ("repost" if "retweeted" in types else "quote" if "quoted" in types
                    else "reply" if "replied_to" in types else "original")
            posts.append({
                "id": row["id"], "author": user.get("name") or username,
                "account": user["username"], "author_id": uid, "platform": "X",
                "timestamp": row["created_at"], "text": (row.get("note_tweet") or {}).get("text") or row["text"],
                "content_kind": kind, "referenced_posts": refs,
                "url": "https://x.com/%s/status/%s" % (username, row["id"]),
                "source": {"kind": "x_api", "reference": ORIGIN + path,
                           "reviewed": True, "account_confirmed": True,
                           "original_time_confirmed": True}})
        return {"status": "OK", "posts": posts,
                "partial": bool(doc.get("meta", {}).get("next_token")),
                "note": "Newest bounded page only; no claim of complete monitoring."}
    except (XUnavailable, TypeError, AttributeError, KeyError):
        # A malformed response, invalid token, denied access or rate limit is unavailable.
        # Keep all exception details private, including any possible secret echoes.
        return {"status": "DATA UNAVAILABLE", "reason": "Official X API unavailable or incomplete", "posts": []}
