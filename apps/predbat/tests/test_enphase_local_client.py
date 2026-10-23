# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Isolated protocol and failure tests for local Enphase gateway control."""

import asyncio
import base64
import copy
import json
import math
import time
import traceback
from unittest.mock import patch

import aiohttp

import enphase_local_client
from enphase_local_client import EnphaseLocalClient, EnphaseLocalError, EnphaseLocalSuperseded


MODE_NAMES = ["ID - Idle", "ZN - Zero Net", "CG - Charge From Grid", "DG - Discharge to Grid", "ND - No Discharge"]


def _state(override=-1, operating=2, soc=54, capacity=15000):
    """Return a gateway scheduler payload with the observed mode-index mapping."""
    return {"acb_current_mode": operating, "acb_debug_mode": override, "agg_soc": soc, "max_energy": capacity, "sched_mode_key": list(MODE_NAMES)}


def _jwt(expires):
    """Build an unsigned fixture token whose expiry is metadata, not authentication proof."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + payload + ".fixture"


class _Response:
    """Provide only the HTTP response interface exercised by the client."""

    def __init__(self, payload=None, status=200, text="", headers=None):
        """Store an isolated body, status and optional redirect headers."""
        self.payload = payload
        self.status = status
        self.body = text
        self.headers = headers or {}

    async def json(self, **kwargs):
        """Return JSON without involving an actual gateway or network connection."""
        if isinstance(self.payload, Exception):
            raise self.payload
        return copy.deepcopy(self.payload)

    async def text(self, **kwargs):
        """Return a fixture body used to check that errors do not expose secrets."""
        return self.body

    async def read(self):
        """Drain a successful command response without treating its body as verified state."""
        return self.body.encode()

    async def __aenter__(self):
        """Enter an awaited response context."""
        return self

    async def __aexit__(self, *args):
        """Finish the response without hiding an exception."""
        return False


class _RequestContext:
    """Emulate aiohttp's awaitable asynchronous request context manager."""

    def __init__(self, outcome):
        """Store a response or exception returned by one request."""
        self.outcome = outcome

    async def _resolve(self):
        """Raise a transport error or provide its response."""
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def __await__(self):
        """Support implementations that await the request before entering its response."""
        return self._resolve().__await__()

    async def __aenter__(self):
        """Support the normal aiohttp asynchronous context-manager pattern."""
        return await self._resolve()

    async def __aexit__(self, *args):
        """Let HTTP and validation exceptions propagate out of the fixture."""
        return False


class _Session:
    """Record actual transport options while serving a finite response sequence."""

    def __init__(self, outcomes):
        """Initialise an unused, open fixture session."""
        self.outcomes = list(outcomes)
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        """Record security-relevant options and provide one predetermined outcome."""
        self.calls.append((method, url, kwargs))
        if not self.outcomes:
            raise AssertionError("Unexpected additional gateway request")
        return _RequestContext(self.outcomes.pop(0))

    async def close(self):
        """Mark the reusable session closed."""
        self.closed = True


def _set_sequence(client, outcomes):
    """Replace the client's request boundary and return its recorded logical calls."""
    calls = []
    remaining = list(outcomes)

    async def request(method, json_body=None):
        """Return one raw gateway result or raise its fixture exception."""
        calls.append((method, json_body))
        if not remaining:
            raise AssertionError("Unexpected additional logical request")
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return copy.deepcopy(outcome)

    client._request = request
    return calls


async def _expect_error(operation):
    """Require a user-safe client error from an asynchronous operation."""
    try:
        await operation
    except EnphaseLocalError as error:
        return error
    raise AssertionError("Expected EnphaseLocalError")


def test_local_client_rejects_unsafe_hosts_and_pins():
    """Credentials must never be sent to a non-HTTPS, injected or redirected authority."""
    invalid_hosts = [
        "http://gateway.example",
        "https://user:password@gateway.example",
        "https://gateway.example/ivp/sc/sched",
        "https://gateway.example?token=fixture",
        "https://gateway.example#fragment",
        "ftp://gateway.example",
        "https://gateway.example:0",
        "https://gateway.example:65536",
        "gateway.example\nInjected: value",
        "https://gateway.example\\@other.example",
    ]
    for host in invalid_hosts:
        try:
            EnphaseLocalClient(host, "fixture-token")
        except (EnphaseLocalError, ValueError):
            continue
        raise AssertionError(f"Unsafe gateway address accepted: {host!r}")
    for fingerprint in ["a" * 63, "a" * 65, "z" * 64]:
        try:
            EnphaseLocalClient("192.0.2.10", "fixture-token", fingerprint=fingerprint)
        except (EnphaseLocalError, ValueError):
            continue
        raise AssertionError("Invalid certificate fingerprint accepted")
    for timeout in [0, -1, math.inf, math.nan]:
        try:
            EnphaseLocalClient("192.0.2.10", "fixture-token", timeout=timeout)
        except (EnphaseLocalError, ValueError):
            continue
        raise AssertionError("Invalid request timeout accepted")


async def test_local_client_normalises_state_and_preserves_unknown_modes():
    """Wh capacity converts to kWh, while unrecognised current modes remain visible."""
    client = EnphaseLocalClient("192.0.2.10", "fixture-token")
    _set_sequence(client, [_state(override=19, operating=22, soc=54.5)])
    result = await client.read_state()
    assert result["capacity_kwh"] == 15
    assert result["soc_percent"] == 54.5
    assert result["override_mode"] == 19
    assert result["operating_mode"] == 22
    assert result["mode_names"] == MODE_NAMES
    await client.close()


async def test_local_client_rejects_invalid_soc_and_firmware_mapping():
    """Control must fail clearly when device units or mode numbers cannot be trusted."""
    for soc in [-1, 101, math.nan, math.inf, None, "unknown"]:
        client = EnphaseLocalClient("192.0.2.10", "fixture-token")
        _set_sequence(client, [_state(soc=soc)])
        await _expect_error(client.read_state())
        await client.close()
    for names in [[], ["Idle", "Charge", "Discharge"], {"1": "Zero Net"}, [MODE_NAMES[0], MODE_NAMES[2], MODE_NAMES[1], *MODE_NAMES[3:]]]:
        client = EnphaseLocalClient("192.0.2.10", "fixture-token")
        payload = _state()
        payload["sched_mode_key"] = names
        calls = _set_sequence(client, [payload])
        await _expect_error(client.set_mode(1))
        assert [method for method, _ in calls] == ["GET"], "Unknown firmware must be rejected before any write"
        await client.close()


async def test_local_client_writes_once_and_verifies_fresh_override():
    """A command is successful only after a new read confirms its override value."""
    client = EnphaseLocalClient("192.0.2.10", "fixture-token")
    calls = _set_sequence(client, [_state(), {}, _state(override=1, operating=2)])
    result = await client.set_mode(1)
    assert calls == [("GET", None), ("POST", {"acb_debug_mode": 1}), ("GET", None)]
    assert result["override_mode"] == 1
    assert result["operating_mode"] == 2, "Readback verifies override acceptance; instantaneous operation may still differ"
    await client.close()


async def test_local_client_guard_rejects_obsolete_mode_before_post():
    """A fresh SOC crossing a control bound supersedes the old mode without a write."""
    client = EnphaseLocalClient("192.0.2.10", "fixture-token")
    calls = _set_sequence(client, [_state(soc=81)])
    error = await _expect_error(client.set_mode(2, guard=lambda state: state["soc_percent"] < 80))
    assert isinstance(error, EnphaseLocalSuperseded)
    assert error.state["soc_percent"] == 81
    assert calls == [("GET", None)], "A superseded decision must not reach the POST boundary"
    await client.close()


async def test_local_client_guard_accepts_fresh_state_and_release_ignores_guard():
    """A current mode is verified normally, while a release has no telemetry guard."""
    client = EnphaseLocalClient("192.0.2.10", "fixture-token")
    observed = []

    def guard(state):
        """Record the exact fresh telemetry used to authorize this command."""
        observed.append(state["soc_percent"])
        return state["soc_percent"] < 80

    calls = _set_sequence(client, [_state(soc=42), {}, _state(override=2, soc=43)])
    state = await client.set_mode(2, guard=guard)
    assert observed == [42]
    assert state["override_mode"] == 2
    assert [method for method, _ in calls] == ["GET", "POST", "GET"]
    calls = _set_sequence(client, [{}, {"acb_debug_mode": -1}])
    released = await client.set_mode(-1, guard=lambda state: False)
    assert released["override_mode"] == -1
    assert calls == [("POST", {"acb_debug_mode": -1}), ("GET", None)]
    await client.close()


async def test_local_client_releases_without_requiring_healthy_telemetry():
    """Returning control to the gateway must remain possible when firmware or telemetry changes."""
    for payload in [_state(override=-1, soc=None), {"acb_debug_mode": -1}, dict(_state(override=-1), sched_mode_key=["unexpected firmware mapping"])]:
        client = EnphaseLocalClient("192.0.2.10", "fixture-token")
        calls = _set_sequence(client, [{}, payload])
        result = await client.set_mode(-1)
        assert result["override_mode"] == -1
        assert calls == [("POST", {"acb_debug_mode": -1}), ("GET", None)]
        await client.close()


async def test_local_client_rejects_unverified_command_numbers():
    """Only the verified forced modes and the explicit release command may be written."""
    for mode in [0, 5, -2, 1.5, True, "1"]:
        client = EnphaseLocalClient("192.0.2.10", "fixture-token")
        calls = _set_sequence(client, [])
        await _expect_error(client.set_mode(mode))
        assert calls == [], "Invalid command reached the gateway"
        await client.close()


async def test_local_client_ambiguous_post_reads_back_without_retry():
    """A timed-out POST may already have landed and must be verified before another action."""
    for failure in [asyncio.TimeoutError(), EnphaseLocalError("Gateway connection failed", ambiguous=True)]:
        client = EnphaseLocalClient("192.0.2.10", "fixture-token")
        calls = _set_sequence(client, [_state(), failure, _state(override=1)])
        result = await client.set_mode(1)
        assert result["override_mode"] == 1
        assert [method for method, _ in calls] == ["GET", "POST", "GET"]
        await client.close()


async def test_local_client_reports_unconfirmed_and_rejected_writes():
    """Unchanged readback and HTTP rejection cannot be reported as applied commands."""
    for outcome in [{}, EnphaseLocalError("Gateway request timed out", ambiguous=True)]:
        client = EnphaseLocalClient("192.0.2.10", "fixture-token")
        calls = _set_sequence(client, [_state(), outcome, _state(override=-1)])
        await _expect_error(client.set_mode(1))
        assert [method for method, _ in calls] == ["GET", "POST", "GET"]
        await client.close()
    client = EnphaseLocalClient("192.0.2.10", "fixture-token")
    calls = _set_sequence(client, [_state(), EnphaseLocalError("Gateway refused access", status=401)])
    error = await _expect_error(client.set_mode(1))
    assert error.status == 401
    assert [method for method, _ in calls] == ["GET", "POST"]
    await client.close()


async def test_local_client_bounds_entire_command_duration():
    """An unresponsive phase cannot outlive the overall command deadline."""
    timeout = 0.03
    client = EnphaseLocalClient("192.0.2.10", "fixture-token", timeout=timeout)
    calls = []

    async def stalled(method, json_body=None):
        """Stall a fixture request until the client's enclosing deadline cancels it."""
        calls.append(method)
        await asyncio.Event().wait()

    client._request = stalled
    started = time.monotonic()
    await _expect_error(client.set_mode(1))
    assert time.monotonic() - started < 0.4, "Command deadline failed to bound a stalled request"
    assert calls == ["GET"]
    await client.close()


async def test_local_client_transport_verifies_tls_and_reuses_session():
    """Bearer requests keep TLS verification and disable redirects on a reused session."""
    session = _Session([_Response(_state()), _Response(_state())])
    with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session) as factory:
        client = EnphaseLocalClient("https://192.0.2.10", "fixture-token")
        await client.read_state()
        await client.read_state()
        assert factory.call_count == 1
        assert len(session.calls) == 2
        for method, url, options in session.calls:
            assert method == "GET" and url == "https://192.0.2.10/ivp/sc/sched"
            assert options["allow_redirects"] is False
            assert options.get("ssl", True) is not False
            assert options["headers"]["Authorization"] == "Bearer fixture-token"
        await client.close()
        assert session.closed


async def test_local_client_transport_uses_explicit_certificate_pin():
    """An approved self-signed certificate uses a SHA256 fingerprint instead of ssl=False."""
    session = _Session([_Response(_state())])
    fingerprint = ":".join(["AB"] * 32)
    with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session):
        client = EnphaseLocalClient("192.0.2.10", "fixture-token", fingerprint=fingerprint)
        await client.read_state()
        assert isinstance(session.calls[0][2]["ssl"], aiohttp.Fingerprint)
        assert session.calls[0][2]["ssl"].fingerprint == bytes.fromhex("ab" * 32)
        await client.close()


async def test_local_client_transport_posts_only_verified_protocol_body():
    """Wire requests use the observed mode override JSON and freshly read back the result."""
    session = _Session([_Response(_state()), _Response(status=204), _Response(_state(override=2))])
    with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session):
        client = EnphaseLocalClient("192.0.2.10", "fixture-token")
        result = await client.set_mode(2)
        assert result["override_mode"] == 2
        assert [method for method, _, _ in session.calls] == ["GET", "POST", "GET"]
        assert session.calls[1][2]["json"] == {"acb_debug_mode": 2}
        assert all(options["allow_redirects"] is False and options["ssl"] is not False for _, _, options in session.calls)
        await client.close()


async def test_local_client_transport_rejects_mismatched_certificate_pin():
    """A failed certificate pin is terminal and cannot silently fall back to unverified TLS."""
    mismatch = aiohttp.ServerFingerprintMismatch(bytes.fromhex("ab" * 32), bytes.fromhex("cd" * 32), "192.0.2.10", 443)
    session = _Session([mismatch])
    with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session):
        client = EnphaseLocalClient("192.0.2.10", "fixture-token", fingerprint="ab" * 32)
        error = await _expect_error(client.read_state())
        assert not error.ambiguous
        assert len(session.calls) == 1
        assert "fixture-token" not in str(error)
        await client.close()


async def test_local_client_transport_rejects_redirects_and_redacts_errors():
    """A redirect or denied response must not leak a bearer token or untrusted response body."""
    secret = "fixture-secret-that-must-never-appear"
    for status in [302, 400, 401, 403, 500]:
        session = _Session([_Response(_state(), status=status, text=secret, headers={"Location": "https://other.example/"})])
        with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session):
            client = EnphaseLocalClient("192.0.2.10", secret)
            error = await _expect_error(client.read_state())
            assert error.status == status
            assert secret not in str(error)
            assert len(session.calls) == 1
            assert session.calls[0][2]["allow_redirects"] is False
            await client.close()
    session = _Session([aiohttp.ClientConnectionError(secret)])
    with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session):
        client = EnphaseLocalClient("192.0.2.10", secret)
        error = await _expect_error(client.read_state())
        assert secret not in str(error)
        await client.close()


async def test_local_client_expired_token_is_not_sent():
    """Known-expired token metadata stops a request without treating unsigned claims as proof."""
    token = _jwt(time.time() - 60)
    session = _Session([_Response(_state())])
    with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session):
        client = EnphaseLocalClient("192.0.2.10", token)
        assert client.token_expires_at is not None
        error = await _expect_error(client.read_state())
        assert token not in str(error)
        assert session.calls == []
        await client.close()


async def test_local_client_future_jwt_claims_do_not_bypass_gateway_authentication():
    """An unsigned future expiry never substitutes for a successful authenticated response."""
    token = _jwt(time.time() + 3600)
    session = _Session([_Response(status=401, text="not authorised")])
    with patch.object(enphase_local_client.aiohttp, "ClientSession", return_value=session):
        client = EnphaseLocalClient("192.0.2.10", token)
        assert client.token_expires_at is not None
        error = await _expect_error(client.read_state())
        assert error.status == 401
        assert len(session.calls) == 1
        assert token not in str(error)
        await client.close()


def test_local_client_ignores_unsupported_jwt_expiry_metadata():
    """Huge finite JWT timestamps cannot break health-attribute date formatting."""
    for expiry in [1e300, -1e300, 253402300800]:
        client = EnphaseLocalClient("192.0.2.10", _jwt(expiry))
        assert client.token_expires_at is None


def run_enphase_local_client_tests(my_predbat):
    """Run standalone client checks through Predbat's existing test registry contract."""
    tests = [function for name, function in sorted(globals().items()) if name.startswith("test_local_client_") and callable(function)]

    async def run():
        """Run every protocol check, recording failures without hiding later outcomes."""
        failed = False
        for function in tests:
            try:
                result = function()
                if asyncio.iscoroutine(result):
                    await result
                print(f"  {function.__name__}: PASSED")
            except Exception:
                failed = True
                print(f"  {function.__name__}: FAILED")
                traceback.print_exc()
        print(f"Local Enphase client: {len(tests)} checks executed")
        return failed

    return asyncio.run(run())
