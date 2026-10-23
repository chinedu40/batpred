# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# -----------------------------------------------------------------------------

"""Bounded, verified battery-mode requests to a local Enphase gateway.

The gateway's schedule endpoint is read back after every write, including an
ambiguous transport failure. A POST is never blindly repeated. Tokens and
response bodies are deliberately absent from error messages.
"""

import asyncio
import base64
from datetime import datetime, timezone
import ipaddress
import json
import math
import re
import time
from urllib.parse import urlsplit

import aiohttp


SCHEDULE_PATH = "/ivp/sc/sched"
EXPECTED_MODE_NAMES = {
    1: "ZN - Zero Net",
    2: "CG - Charge From Grid",
    3: "DG - Discharge to Grid",
    4: "ND - No Discharge",
}
WRITABLE_MODES = {-1, 1, 2, 3, 4}


class EnphaseLocalError(Exception):
    """A gateway failure described without credentials or response contents."""

    def __init__(self, message, status=None, ambiguous=False):
        """Retain an optional HTTP status and whether a write may have landed."""
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class EnphaseLocalSuperseded(EnphaseLocalError):
    """A fresh state or newer control decision prevented an obsolete mode write."""

    def __init__(self, state):
        """Expose normalized fresh state so the caller can recompute its decision."""
        super().__init__("Enphase local mode request was superseded before the write")
        self.state = dict(state)


def _gateway_url(host):
    """Validate a gateway authority and return its HTTPS origin."""
    if not isinstance(host, str) or not host or host != host.strip() or any(char.isspace() for char in host):
        raise EnphaseLocalError("Enphase local host must be a hostname or HTTPS authority")
    if any(char in host for char in ("?", "#", "\\")):
        raise EnphaseLocalError("Enphase local host must not contain a path, query or fragment")
    candidate = host
    if "://" not in candidate:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            address = None
        if isinstance(address, ipaddress.IPv6Address):
            candidate = f"[{address}]"
        candidate = "https://" + candidate
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
        invalid = parsed.scheme != "https" or not hostname or parsed.username is not None or parsed.password is not None or bool(parsed.path) or bool(parsed.query) or bool(parsed.fragment)
    except ValueError:
        raise EnphaseLocalError("Enphase local host is not a valid HTTPS authority") from None
    if invalid:
        raise EnphaseLocalError("Enphase local host requires HTTPS without user information or a path")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
        if len(hostname) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in hostname.rstrip(".").split(".")):
            raise EnphaseLocalError("Enphase local host is not a valid hostname or IP address") from None
    if port is not None and not 1 <= port <= 65535:
        raise EnphaseLocalError("Enphase local port must be between 1 and 65535")
    authority = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else hostname
    return "https://" + authority + (f":{port}" if port is not None else "")


def _certificate_fingerprint(fingerprint):
    """Parse an optional SHA-256 certificate pin without weakening TLS checks."""
    if fingerprint is None or fingerprint == "":
        return True
    if not isinstance(fingerprint, str):
        raise EnphaseLocalError("Enphase local fingerprint must be a SHA-256 hexadecimal digest")
    normalized = fingerprint.strip()
    if normalized.lower().startswith("sha256:"):
        normalized = normalized[7:]
    normalized = normalized.replace(":", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", normalized):
        raise EnphaseLocalError("Enphase local fingerprint must contain exactly 32 SHA-256 bytes")
    return aiohttp.Fingerprint(bytes.fromhex(normalized))


def _token_expiry(token):
    """Read an unsigned JWT expiry as metadata, never as authentication proof."""
    try:
        segments = token.split(".")
        if len(segments) != 3:
            return None
        payload = segments[1] + "=" * (-len(segments[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        expiry = claims.get("exp") if isinstance(claims, dict) else None
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            return None
        expiry = float(expiry)
        # This metadata is also rendered in health attributes. A finite number
        # can still be outside datetime's supported years (for example 1e300).
        datetime.fromtimestamp(expiry, timezone.utc)
        return expiry
    except (ValueError, TypeError, UnicodeError, OverflowError, OSError):
        return None


def _integer(value):
    """Accept only actual JSON integers, retaining unknown device mode values."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value, minimum, maximum=None):
    """Return a finite JSON number in range, or None for an invalid value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
    except OverflowError:
        valid = False
    if not valid:
        return None
    return float(value)


def _normalize_state(payload, release=False):
    """Validate gateway state, allowing partial telemetry when releasing a mode."""
    if not isinstance(payload, dict):
        raise EnphaseLocalError("Enphase local schedule response must be an object")
    override = _integer(payload.get("acb_debug_mode"))
    operating = _integer(payload.get("acb_current_mode"))
    soc = _number(payload.get("agg_soc"), 0, 100)
    names = payload.get("sched_mode_key")
    valid_names = isinstance(names, list) and all(isinstance(name, str) for name in names)
    known_mapping = valid_names and len(names) > max(EXPECTED_MODE_NAMES) and all(names[index].strip().casefold() == expected.casefold() for index, expected in EXPECTED_MODE_NAMES.items())
    if override is None or (not release and operating is None):
        raise EnphaseLocalError("Enphase local schedule response has an invalid battery mode")
    if not release and soc is None:
        raise EnphaseLocalError("Enphase local schedule response has an invalid battery state of charge")
    if not release and not known_mapping:
        raise EnphaseLocalError("Enphase local firmware mode mapping is unsupported; no mode will be written")
    capacity_wh = _number(payload.get("max_energy"), 0)
    capacity = capacity_wh / 1000 if capacity_wh is not None and capacity_wh > 0 else None
    return {"override_mode": override, "operating_mode": operating, "soc_percent": soc, "capacity_kwh": capacity, "mode_names": list(names) if valid_names else None}


class EnphaseLocalClient:
    """HTTPS gateway client confined to its owning asynchronous event loop."""

    def __init__(self, host, token, fingerprint=None, timeout=5):
        """Validate connection settings without opening a network connection."""
        self.base_url = _gateway_url(host)
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise EnphaseLocalError("Enphase local token must be a nonempty bearer token without whitespace")
        checked_timeout = _number(timeout, 0)
        if checked_timeout is None or checked_timeout == 0:
            raise EnphaseLocalError("Enphase local timeout must be a positive finite number")
        self.timeout = checked_timeout
        self.token_expires_at = _token_expiry(token)
        self._token = token
        self._ssl = _certificate_fingerprint(fingerprint)
        self._session = None
        self._loop = None

    async def _get_session(self):
        """Create or reuse a session on its owning loop, without proxy or cookies."""
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise EnphaseLocalError("Enphase local client must be used on its owning event loop")
        if self._session is None or self._session.closed:
            self._loop = loop
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        return self._session

    async def _request(self, method, json_body=None):
        """Make one bounded request with redirects disabled and sanitized failures."""
        started = time.monotonic()
        if self.token_expires_at is not None and self.token_expires_at <= time.time():
            raise EnphaseLocalError("Enphase local access token has expired; provide a fresh gateway token", status=401)
        session = await self._get_session()
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        try:
            async with session.request(method, self.base_url + SCHEDULE_PATH, headers=headers, json=json_body, ssl=self._ssl, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                elapsed = time.monotonic() - started
                if 300 <= response.status < 400:
                    raise EnphaseLocalError(f"Enphase local {method} redirect refused after {elapsed:.2f}s", status=response.status)
                if not 200 <= response.status < 300:
                    raise EnphaseLocalError(f"Enphase local {method} returned HTTP {response.status} after {elapsed:.2f}s", status=response.status)
                if method == "POST":
                    await response.read()
                    return {}
                try:
                    return await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    raise EnphaseLocalError(f"Enphase local {method} returned invalid JSON after {time.monotonic() - started:.2f}s", status=response.status) from None
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError, aiohttp.ServerFingerprintMismatch):
            raise EnphaseLocalError(f"Enphase local {method} certificate verification failed after {time.monotonic() - started:.2f}s") from None
        except asyncio.TimeoutError:
            raise EnphaseLocalError(f"Enphase local {method} timed out after {time.monotonic() - started:.2f}s", ambiguous=method == "POST") from None
        except (aiohttp.ClientError, OSError):
            raise EnphaseLocalError(f"Enphase local {method} connection failed after {time.monotonic() - started:.2f}s", ambiguous=method == "POST") from None

    async def read_state(self):
        """Read fresh battery state and reject firmware maps unsafe for forced modes."""
        return _normalize_state(await self._request("GET"))

    async def set_mode(self, mode, guard=None):
        """Write once and verify by reading back, within twice the request timeout.

        A forced mode requires a valid fresh state and known firmware mapping first.
        An optional synchronous guard rechecks the caller's current decision from
        that fresh state immediately before the POST, without yielding in between.
        Releasing the override (-1) deliberately bypasses those telemetry gates so
        missing SOC or a changed firmware map cannot strand our forced mode.
        """
        if _integer(mode) is None or mode not in WRITABLE_MODES:
            raise EnphaseLocalError("Enphase local requested mode must be -1, 1, 2, 3 or 4")
        post_started = False
        try:
            async with asyncio.timeout(2 * self.timeout):
                if mode != -1:
                    fresh_state = await self.read_state()
                    if guard is not None and not guard(fresh_state):
                        raise EnphaseLocalSuperseded(fresh_state)
                post_started = True
                try:
                    await self._request("POST", {"acb_debug_mode": mode})
                except EnphaseLocalError as error:
                    if not error.ambiguous:
                        raise
                except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                    # Also supports injected request implementations: a timed-out
                    # POST may have landed, so read back instead of repeating it.
                    pass
                state = _normalize_state(await self._request("GET"), release=mode == -1)
                if state["override_mode"] != mode:
                    raise EnphaseLocalError("Enphase local mode write was not confirmed by gateway read-back")
                return state
        except asyncio.TimeoutError:
            raise EnphaseLocalError(f"Enphase local mode transaction timed out after {2 * self.timeout:.2f}s; outcome unconfirmed", ambiguous=post_started) from None

    async def close(self):
        """Close the session on its owner loop; repeated closes are harmless."""
        if self._session is not None and not self._session.closed:
            if self._loop is not asyncio.get_running_loop():
                raise EnphaseLocalError("Enphase local client must be closed on its owning event loop")
            await self._session.close()
        self._session = None
        self._loop = None
