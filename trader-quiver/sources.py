"""Bounded, read-only Quiver transports. Python 3.8+, standard library only."""

import os
import urllib.error
import urllib.request

PUBLIC_URLS = {
    "CONGRESS": "https://www.quiverquant.com/congresstrading/",
    "INSIDER": "https://www.quiverquant.com/insiders/",
}
# Paths and Token authentication follow Quiver-Quantitative/python-api.
API_URLS = {
    "CONGRESS": "https://api.quiverquant.com/beta/live/congresstrading",
    "INSIDER": "https://api.quiverquant.com/beta/live/insiders",
}
MAX_BYTES = 8 * 1024 * 1024


class SourceUnavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential, follow a login, or use another endpoint.
        raise SourceUnavailable("Redirect refused; endpoint or access policy changed")


def _get(url, token=None):
    if url not in tuple(PUBLIC_URLS.values()) + tuple(API_URLS.values()):
        raise SourceUnavailable("Only the fixed Quiver endpoints are supported")
    if token is not None and url not in API_URLS.values():
        raise SourceUnavailable("API credentials cannot be sent to public webpages")
    headers = {"User-Agent": "trader-quiver/1.0 (public disclosure reader)",
               "Accept": "application/json" if token is not None else "text/html"}
    if token is not None:
        headers["Authorization"] = "Token " + token
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        # Preserve inherited proxy/CA settings and TLS verification. No cookies.
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            if response.getcode() != 200:
                raise SourceUnavailable("Quiver returned an unexpected HTTP status")
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise SourceUnavailable("Quiver response exceeds the 8 MiB limit")
        return raw.decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            message = "Quiver HTTP 429; rate limited, stop and retry later"
        elif exc.code in (401, 403):
            message = "Quiver HTTP %d; access denied, no login or paywall bypass" % exc.code
        else:
            message = "Quiver HTTP %d; data unavailable" % exc.code
        raise SourceUnavailable(message) from None
    except (OSError, ValueError, UnicodeError):
        # Do not echo request headers, response bodies, URLs or credentials.
        raise SourceUnavailable("Quiver connection, TLS or response encoding unavailable") from None


def fetch_public(dataset):
    if dataset not in PUBLIC_URLS:
        raise SourceUnavailable("Unsupported public dataset")
    # Deliberately does not read QUIVER_API_KEY. One page, no pagination/retries.
    url = PUBLIC_URLS[dataset]
    return {"source": "PUBLIC_WEB", "reference": url, "body": _get(url)}


def fetch_api(dataset):
    if dataset not in API_URLS:
        raise SourceUnavailable("Unsupported API dataset")
    token = os.environ.get("QUIVER_API_KEY", "").strip()
    if not token:
        raise SourceUnavailable("QUIVER_API_KEY missing; API was not contacted")
    url = API_URLS[dataset]
    return {"source": "QUIVER_API", "reference": url, "body": _get(url, token)}
