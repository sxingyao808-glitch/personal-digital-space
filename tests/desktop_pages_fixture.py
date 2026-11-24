"""Opt-in Cloudflare simulator for the isolated Electron acceptance runner."""

import os
from pathlib import Path

from archive import app as archive_app
from test_cloudflare_publishing import PagesSimulator


def install_fixture():
    mode_path = Path(os.environ["PDS_TEST_PAGES_MODE_FILE"])
    create_app = archive_app.create_app
    remote = PagesSimulator()

    def simulated_request(*args, **kwargs):
        remote.failure = mode_path.read_text(encoding="utf-8").strip()
        return PagesSimulator.request(remote, *args, **kwargs)

    remote.request = simulated_request

    def with_simulator(*args, **kwargs):
        app = create_app(*args, **kwargs)
        hosting = app.extensions["pages_hosting"]
        hosting.client_factory = remote.client
        hosting.poll_delay = 0
        app.add_url_rule("/__pages_test_fixture", "pages_test_fixture", lambda: "synthetic-pages-fixture")
        return app

    archive_app.create_app = with_simulator
