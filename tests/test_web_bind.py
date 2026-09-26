"""The bind address, asserted against the socket rather than against the argument.

The plan states this as an invariant: *the server binds `127.0.0.1`, asserted
against the actually-bound socket.* That wording is the test. A test that read
`args.host` back would pass on a server listening on every interface, because the
string and the socket are two different facts and only one of them is what the
kernel did.

So every assertion below comes from `getsockname()` or from a connection that
either was or was not accepted -- never from what the code was asked for.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from examkb import cli
from examkb.web import serve

# Every way of spelling "listen on more than this machine" that anyone would
# plausibly type, plus the two that arrive by accident (empty string, a bare port).
ROUTABLE = ["0.0.0.0", "", "::", "192.168.1.10", "10.0.0.1", "8.8.8.8"]
LOOPBACK = ["127.0.0.1", "127.0.0.2", "localhost", "::1"]


# ------------------------------------------------------------------- the guard itself


@pytest.mark.parametrize("host", ROUTABLE)
def test_a_routable_address_is_refused(host: str) -> None:
    with pytest.raises(serve.BindRefused) as raised:
        serve.bind(host, 0)
    assert "not a loopback address" in str(raised.value)


@pytest.mark.parametrize("host", ROUTABLE)
def test_a_refusal_names_the_decision_that_would_change_it(host: str) -> None:
    """The refusal is an explanation, not a wall: D1 is where the answer lives."""
    with pytest.raises(serve.BindRefused) as raised:
        serve.bind(host, 0)
    message = str(raised.value)
    assert "D1" in message and "issues/decisions/D1-portability.md" in message


@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_spellings_are_accepted_and_bind_loopback(host: str) -> None:
    bound = serve.bind(host, 0)
    try:
        # The address the kernel gave, not the one we asked for.
        assert serve.is_loopback(bound.socket.getsockname()[0])
        assert bound.host == bound.socket.getsockname()[0]
    finally:
        bound.close()


def test_a_refused_bind_leaves_no_socket_listening() -> None:
    """A refusal must not leave the port half-open on the way out."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(serve.BindRefused):
        serve.bind("0.0.0.0", port)

    # If the refused bind had leaked a listening socket, this would fail.
    again = serve.bind("127.0.0.1", port)
    again.close()


def test_zero_zero_zero_zero_is_not_loopback_even_though_you_can_reach_it() -> None:
    """The trap this guard exists for.

    Connecting to 0.0.0.0 from this box works, so "can I reach it locally" is not
    the question. The question is which interfaces it accepts from, and 0.0.0.0
    means all of them.
    """
    assert not serve.is_loopback("0.0.0.0")
    assert not serve.is_loopback("::")
    assert serve.is_loopback("127.0.0.1")
    assert serve.is_loopback("::1")


def test_the_post_bind_check_would_catch_a_socket_bound_elsewhere(monkeypatch) -> None:
    """The second check is not decoration -- prove it fires on its own.

    `resolve` is neutered so a routable address gets past the pre-bind check. The
    bind then really does listen on 0.0.0.0, and the `getsockname()` check is the
    only thing left standing between that socket and uvicorn.
    """
    monkeypatch.setattr(
        serve, "resolve", lambda host, port: (socket.AF_INET, ("0.0.0.0", port))
    )
    with pytest.raises(serve.BindRefused) as raised:
        serve.bind("pretend-loopback", 0)
    assert "0.0.0.0" in str(raised.value)


def test_a_port_already_in_use_is_a_sentence_not_a_traceback() -> None:
    held = serve.bind("127.0.0.1", 0)
    try:
        with pytest.raises(serve.BindRefused) as raised:
            serve.bind_or_explain("127.0.0.1", held.port)
        assert "already in use" in str(raised.value)
        assert "--port" in str(raised.value)
    finally:
        held.close()


def test_bound_url_is_what_you_would_paste_in_a_browser() -> None:
    bound = serve.bind("127.0.0.1", 0)
    try:
        assert bound.url == f"http://127.0.0.1:{bound.port}"
    finally:
        bound.close()

    bound = serve.bind("::1", 0)
    try:
        assert bound.url == f"http://[::1]:{bound.port}"
    finally:
        bound.close()


# ---------------------------------------------------------------- the running server


@pytest.fixture
def running_server():
    """The real app, on a real socket, in a thread. Yields the `Bound`."""
    import uvicorn

    from examkb.web.app import create_app

    bound = serve.bind("127.0.0.1", 0)
    config = uvicorn.Config(create_app(), log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [bound.socket]}, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    try:
        yield bound, server
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        bound.close()


def _local_addresses() -> set[str]:
    """Every address this box answers on, however it can be discovered.

    `gethostname()` alone is not enough: under WSL it often resolves to loopback
    only, and a test that silently skips is a test that stopped guarding anything.
    The UDP socket sends no packets -- connect() on a datagram socket only picks
    the route, which is what names the outward-facing address.
    """
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM):
            found.add(str(info[4][0]))
    except socket.gaierror:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routed nowhere, contacted never
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return found



def test_the_server_uvicorn_started_is_listening_on_loopback(running_server) -> None:
    """`server.sockets[0].getsockname()` -- the criterion, word for word."""
    _bound, server = running_server
    sockets = [sock for srv in server.servers for sock in srv.sockets]
    assert sockets, "uvicorn reported no sockets"
    for sock in sockets:
        host, _port = sock.getsockname()[:2]
        assert serve.is_loopback(str(host)), f"uvicorn is listening on {host}"


def test_the_running_server_answers_on_loopback(running_server) -> None:
    bound, _server = running_server
    with urllib.request.urlopen(f"{bound.url}/healthz", timeout=10) as response:
        assert response.status == 200
        assert b'"status":"ok"' in response.read().replace(b", ", b",")


def test_the_running_server_refuses_a_connection_on_the_lan_address(running_server) -> None:
    """The other half of the invariant: nothing outside this box can reach it.

    Tried against every non-loopback address this machine actually has. If the
    server were on 0.0.0.0, one of these would connect.
    """
    bound, _server = running_server
    addresses = {a for a in _local_addresses() if not serve.is_loopback(a)}
    if not addresses:
        pytest.skip("this box has no non-loopback address to try")

    for address in addresses:
        with pytest.raises(OSError):
            with socket.create_connection((address, bound.port), timeout=2):
                pass


# ------------------------------------------------------------------------ the command
#
# One subprocess, not four. 007 flagged the fast suite drifting upward on the back
# of tests that spawn a real `examkb`; the thing only a subprocess can prove here is
# that `serve` is registered under `python -m examkb.cli` (006's registry bug), and
# one test proves that. The rest drive `cli.main()` in process, which exercises the
# same argparse wiring in microseconds.


def test_serve_is_registered_under_python_m(tmp_path: Path) -> None:
    """The one subprocess: `python -m examkb.cli` has its own module registry."""
    result = subprocess.run(
        [sys.executable, "-m", "examkb.cli", "serve", "--port", "0", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "http://127.0.0.1:" in result.stdout


def test_serve_check_binds_and_reports_the_address(capsys) -> None:
    assert cli.main(["serve", "--port", "0", "--check"]) == 0
    assert "http://127.0.0.1:" in capsys.readouterr().out


@pytest.mark.parametrize("host", ROUTABLE[:4])
def test_serve_exits_non_zero_on_a_routable_host(host: str, capsys) -> None:
    assert cli.main(["serve", "--host", host, "--port", "0", "--check"]) == 1
    errors = capsys.readouterr().err
    assert "not a loopback address" in errors
    assert "D1" in errors


def test_serve_has_no_reload_flag(capsys) -> None:
    """Deliberately absent: 030's backfills run for hours and a reloader kills them."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["serve", "--reload", "--check"])
    assert raised.value.code != 0
    assert "unrecognized arguments" in capsys.readouterr().err
