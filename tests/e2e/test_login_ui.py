"""Browser test of the owner login against a real server with the login switched on."""
import os
import subprocess
import sys
import time

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

import fakes  # noqa: E402
from conftest import FFMPEG  # noqa: E402
from test_ui import ROOT, free_port  # noqa: E402
from test_visuals_ui import chromium  # noqa: E402,F401  (fixture)

from app import auth  # noqa: E402

PASSWORD = "browser test password"


@pytest.fixture(scope="module")
def secured_stack(tmp_path_factory):
    fake = fakes.start_fake()
    port = free_port()
    env = dict(
        os.environ,
        GROQ_API_KEY="test-key-not-real",
        GROQ_API_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/audio/transcriptions",
        OLLAMA_HOST_URL="http://127.0.0.1:1",
        USE_GROQ_CHAT="0",
        DATA_DIR=str(tmp_path_factory.mktemp("login-data")),
        FFMPEG_BIN=FFMPEG or "ffmpeg",
        PORT=str(port),
        HOST="127.0.0.1",
        VOICE_NOTES_AUTH="on",
        VOICE_NOTES_PASSWORD_HASH=auth.hash_password(PASSWORD),
    )
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/login", timeout=1, follow_redirects=False).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


def test_sign_in_use_the_app_and_sign_out(chromium, secured_stack):
    ctx = chromium.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(secured_stack + "/")
    assert page.url.endswith("/login")  # a visitor is sent to the login page
    expect(page.locator("#password")).to_be_visible()

    page.fill("#password", "wrong password")
    page.click("button[type=submit]")
    page.locator(".field-error", has_text="Wrong password").wait_for()
    assert page.url.endswith("/login")

    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("#view-start:not([hidden])")
    assert not page.url.endswith("/login")
    cookies = {c["name"]: c for c in ctx.cookies()}
    assert cookies[auth.COOKIE_NAME]["httpOnly"] is True and cookies[auth.COOKIE_NAME]["sameSite"] == "Lax"

    page.click("#logout")
    page.wait_for_url("**/login")
    page.goto(secured_stack + "/")
    assert page.url.endswith("/login")  # the session is gone
    ctx.close()


def test_a_session_that_ended_sends_the_page_to_the_login(chromium, secured_stack):
    ctx = chromium.new_context()
    page = ctx.new_page()
    page.goto(secured_stack + "/login")
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("#view-start:not([hidden])")
    ctx.clear_cookies()
    page.goto(secured_stack + "/#/tasks")
    page.wait_for_url("**/login")
    ctx.close()
