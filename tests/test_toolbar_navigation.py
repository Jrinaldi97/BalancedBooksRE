"""A sidebar link works after a toolbar button (2.18.0 gate, macbase1 NEW-9).

The toolbar's Home, Quick Entry and Reports called App.navigate() without
changing the address. The page changed, the hash did not, so the sidebar link
of the page just left did nothing: clicking it set the hash it already had,
and no hashchange fired. Settings -> toolbar Home -> sidebar Settings stayed
on the Dashboard until some other page was visited. The keyboard shortcuts,
search results and pages that move on by themselves had the same gap.

App.navigate now keeps the address in step (history.pushState, which gives
Back an entry and fires no second navigation), whoever calls it. The probe
drives app.js and bootstrap.js on a page whose address behaves like a
browser's.
"""

import functools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


@functools.lru_cache(maxsize=None)
def _steps():
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "toolbar_nav_probe.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",  # node writes UTF-8; Windows would read cp1252
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return {s["step"]: s for s in map(json.loads, out.stdout.splitlines())}


@needs_node
def test_a_toolbar_button_moves_the_address_with_the_page():
    steps = _steps()
    assert steps["start-on-settings"]["rendered"] == ["SettingsPage"]
    for step, page, address in (
        ("toolbar-home", "PortfolioPage", "#/"),
        ("toolbar-quick-entry", "QuickEntry", "#/quick-entry"),
        ("toolbar-reports", "ReportsPage", "#/reports"),
    ):
        assert steps[step]["rendered"] == [page], step
        assert steps[step]["hash"] == address, step


@needs_node
def test_the_sidebar_link_of_the_page_left_still_works():
    steps = _steps()
    # macbase1's two routes, and Quick Entry's
    assert steps["sidebar-settings"]["rendered"] == ["SettingsPage"]
    assert steps["sidebar-settings-again"]["rendered"] == ["SettingsPage"]
    assert steps["sidebar-accounts-again"]["rendered"] == ["ChartOfAccounts"]


@needs_node
def test_back_returns_to_the_page_before():
    back = _steps()["back"]
    assert back["hash"] == "#/reports" and back["rendered"] == ["ReportsPage"]


@needs_node
def test_an_old_check_register_bookmark_leaves_no_step_back_into_itself():
    old = _steps()["old-check-register-bookmark"]
    assert old["hash"] == "#/banking" and old["rendered"] == ["BankingPage"]
    # Back from Banking goes where the person was, not to the alias that
    # would send them forward again
    assert "#/check-register" not in old["back"]
