"""Small Market Data HTTPS client with injectable transport and bounded retries."""
import datetime as dt
import email.utils
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(Exception):
    def __init__(self, status, http_status=None, retry_after=None):
        super().__init__(status)  # Never retain response bodies/URLs/credentials.
        self.status, self.http_status = status, http_status
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Auth must never follow a redirect to another service.


def urlopen_transport(url, params, headers):
    query = urllib.parse.urlencode(params)
    target = url + (("&" if "?" in url else "?") + query if query else "")
    request = urllib.request.Request(target, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), b""  # Do not archive credential-bearing errors.


class HTTPClient:
    def __init__(self, host, transport=None, sleep=time.sleep, monotonic=time.monotonic,
                 retries=3, requests_per_minute=180, max_backoff=30):
        if not 0 <= retries <= 6 or requests_per_minute <= 0 or not 0 < max_backoff <= 60:
            raise ValueError("invalid bounded HTTP policy")
        self.host = host
        self.transport = transport or urlopen_transport
        self.sleep, self.monotonic = sleep, monotonic
        self.retries, self.max_backoff = retries, max_backoff
        self.interval, self.last_call = 60 / requests_per_minute, None
        self.events = []

    def get(self, url, params, headers):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != self.host or parsed.username or parsed.fragment:
            raise ProviderError("UNSAFE_PAGINATION_URL")
        for attempt in range(self.retries + 1):
            if self.last_call is not None:
                self.sleep(max(0, self.interval - (self.monotonic() - self.last_call)))
            self.last_call = self.monotonic()
            try:
                status, response_headers, body = self.transport(url, params, headers)
            except (urllib.error.URLError, OSError, TimeoutError):
                status, response_headers, body = None, {}, b""
            self.events.append(dict(http_status=status, attempt=attempt + 1))
            if status in (401, 403):
                raise ProviderError("AUTH_ERROR", status)
            if status is not None and 200 <= status < 300:
                try:
                    value = json.loads(body) if isinstance(body, (bytes, str)) else body
                    if not isinstance(value, dict):
                        raise ValueError()
                    return value
                except (ValueError, TypeError):
                    raise ProviderError("INVALID_RESPONSE", status)
            retryable = status is None or status == 429 or status >= 500
            reason = "RATE_LIMITED" if status == 429 else "PROVIDER_ERROR"
            if not retryable:
                raise ProviderError(reason, status)
            delay = min(self.max_backoff, 2 ** attempt)
            retry_after = next((v for k, v in response_headers.items() if k.lower() == "retry-after"), None)
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        when = email.utils.parsedate_to_datetime(retry_after)
                        delay = max(delay, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
                    except (TypeError, ValueError):
                        pass
            if attempt == self.retries or delay > self.max_backoff:
                # Preserve checkpoint; caller can resume later. Never retry early.
                raise ProviderError(reason, status, delay if status == 429 else None)
            self.sleep(delay)
