"""Browser regressions for the two client-side bugs on the onboarding path.

`parseDataset` (the "Score my traces" door) and the Connect snippet are both
client-side, and a JSONL file whose rows are objects also begins with `{` —
so the single-object branch swallowed the whole multi-line string. The shipped
3-row sample is exactly that shape, which meant step 2 of the empty state
failed with a raw parser error. The snippet meanwhile hardcoded localhost, so
visitors to the deployed host were told to curl their own machine.

These run only where Playwright is installed (the project's dev interpreter
has it); everywhere else they skip rather than fail. A source-grep would not
have caught either bug — the path is the browser.
"""

import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest


pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from playwright.sync_api import sync_playwright  # noqa: E402

from agentcheck.proxy import create_app  # noqa: E402
from agentcheck.store import Store  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    """A real uvicorn server on a real port — the point of the test is
    that the page learns its own origin, which TestClient cannot provide."""

    def __init__(self):
        self.port = _free_port()
        store = Store(Path(tempfile.mkdtemp()) / "ui.db")
        self.key = store.create_key("ui", qpm_limit=600)
        app = create_app(store, default_judge="stub")
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1)
                return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("test server did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def ui_server():
    with _Server() as s:
        yield s


@pytest.fixture(scope="module")
def base_url(ui_server):
    return f"http://127.0.0.1:{ui_server.port}"


def test_sample_upload_scores_three_rows(base_url):
    """The shipped sample is JSONL; it must reach the JSONL branch and score."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(base_url, wait_until="networkidle", timeout=20000)
        pg.click("#open-upload")
        pg.click("#load-sample")
        pg.click("#run-upload")
        for _ in range(60):
            time.sleep(0.25)
            status = pg.evaluate("()=>document.querySelector('#upload-status').textContent")
            if status and not status.startswith("Scoring"):
                break
        rows = pg.evaluate(
            "()=>document.querySelectorAll('#upload-out tbody tr').length")
        b.close()
    assert "Scored 3" in status, f"sample did not score: {status}"
    assert rows == 3, f"expected 3 scored rows, got {rows}"


def test_connect_snippet_uses_served_origin(base_url):
    """The snippet must name the host the page was served from, never 7373."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(base_url, wait_until="networkidle", timeout=20000)
        pg.click("#open-connect")
        snippet = pg.evaluate("()=>document.querySelector('#snippet').textContent")
        first = snippet.splitlines()[0]
        origin = pg.evaluate("()=>location.origin")
        b.close()
    assert "127.0.0.1:7373" not in snippet, "snippet must not hardcode localhost"
    assert origin in snippet, f"snippet must use the served origin: {first}"


def test_nav_is_five_tabs_and_trust_holds_three(base_url):
    """Three top-level tabs once began with "Trust" (Trust / Trust Report /
    Trust Under Attack), so the product's central concept was the hardest to
    find. The score, its history, and its adversarial result are one tab now."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(base_url, wait_until="networkidle", timeout=20000)
        nav = pg.evaluate("()=>[...document.querySelectorAll('#nav button')].map(b=>b.textContent)")
        assert nav == ["Decision Log", "Runs", "Rubrics", "Policies", "Trust"], nav
        assert len([n for n in nav if n.startswith("Trust")]) == 1, \
            "exactly one top-level tab may begin with Trust"

        pg.click('[data-view="trust"]')
        tabs = pg.evaluate(
            "()=>[...document.querySelectorAll('[data-trusttab]')].map(b=>b.textContent)")
        assert tabs == ["Score", "Over time", "Under attack"], tabs

        # Each sub-tab reveals its own panel and hides the others.
        for want in ("score", "report", "attack"):
            pg.click(f'[data-trusttab="{want}"]')
            shown = pg.evaluate("""()=>['score','report','attack']
                .filter(t=>!document.getElementById('trust-panel-'+t).hidden)""")
            assert shown == [want], f"{want}: shown={shown}"
        # The active segment must carry aria-pressed, which is what the active
        # style keys on — a bare `.on` class left the selection unstyled.
        pressed = pg.evaluate("""()=>[...document.querySelectorAll('[data-trusttab]')]
            .filter(b=>b.getAttribute('aria-pressed')==='true').map(b=>b.textContent)""")
        assert pressed == ["Under attack"], pressed
        b.close()


def test_signoff_closes_the_loop(base_url):
    """Reviewing must visibly change something. The hint line used to read the
    same whether you had signed out 0 or 30 calls — which is why the third step
    of the empty state had no payoff. Abstentions carry no signal, so only
    agree/miss move the percentage."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.goto(base_url, wait_until="networkidle", timeout=20000)
        pg.click("#open-upload")
        pg.click("#load-sample")
        pg.click("#run-upload")
        for _ in range(40):
            time.sleep(0.25)
            if not pg.evaluate(
                    "()=>document.querySelector('#upload-status').textContent").startswith("Scoring"):
                break
        pg.click("#sheet-upload .x")
        ids = pg.evaluate("()=>[...document.querySelectorAll('#rows .row')].map(r=>r.dataset.id)")

        def sign_out(i, verdict):
            pg.click(f'#rows .row[data-id="{ids[i]}"]')
            pg.click(f'.signoff .btn[data-assess="{verdict}"]')
            time.sleep(0.4)

        hint = lambda: pg.evaluate("()=>document.querySelector('#hintline').textContent")

        assert "signed out" not in hint()
        sign_out(0, "looks_correct")
        assert "1 signed out, judge agreed on 100%" in hint(), hint()
        sign_out(1, "actual_issue")
        assert "2 signed out, judge agreed on 50%" in hint(), hint()
        sign_out(2, "insufficient_context")
        # An abstention is neither agreement nor a miss: the count rises, the
        # percentage does not move.
        assert "3 signed out, judge agreed on 50%" in hint(), hint()
        b.close()


def test_key_gate_for_hosted_instances(base_url, ui_server):
    """A non-demo instance does not hand out a key. The hosted UI used to show
    'Is agentcheck serve running?' and offered no way in, which made every
    non-demo deployment API-only in the browser."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        # Simulate the hosted case: bootstrap refuses.
        pg.route("**/v1/bootstrap", lambda r: r.fulfill(
            status=403, content_type="application/json",
            body='{"detail":"browser auto-login is only for localhost"}'))
        pg.goto(base_url, wait_until="networkidle", timeout=20000)

        assert not pg.evaluate("()=>document.querySelector('#sheet-key').hidden"), \
            "the key sheet must open when bootstrap is refused"
        body = pg.evaluate("()=>document.body.textContent")
        assert "serve running" not in body, "must not claim the server is down"

        # A wrong key is rejected in place, not on the next page load.
        pg.fill("#key-input", "ac_definitely-wrong")
        pg.click("#use-key")
        for _ in range(20):
            time.sleep(0.15)
            if "not recognised" in pg.evaluate(
                    "()=>document.querySelector('#key-status').textContent"):
                break
        assert "not recognised" in pg.evaluate(
            "()=>document.querySelector('#key-status').textContent")

        # The issued key gets in, and survives a reload for this tab.
        pg.fill("#key-input", ui_server.key)
        pg.click("#use-key")
        for _ in range(40):
            time.sleep(0.15)
            if pg.evaluate("()=>!document.querySelector('#sheet-key').hidden") is False:
                break
        assert pg.evaluate("()=>document.querySelector('#sheet-key').hidden")
        pg.reload(wait_until="networkidle")
        time.sleep(0.8)
        assert pg.evaluate("()=>document.querySelector('#sheet-key').hidden"), \
            "a key held for the tab must not re-gate on reload"
        b.close()
