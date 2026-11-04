"""Clicks through the whole app with the demo platform:  python -m pytest tests/test_app.py"""
import os
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                                        # backups/profiles go to a temp folder
    monkeypatch.setenv("CMT_EXTRA_PLUGINS", "demo=tests.fake_platform:FakeConnector")
    monkeypatch.syspath_prepend(str(ROOT))
    from tests import fake_platform
    fake_platform.STORE.unlink(missing_ok=True)
    for k in [k for k in os.environ if k.startswith(("SRC_", "TGT_"))]:
        monkeypatch.delenv(k)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].set_value("demo").run()
    return at


def go(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception


def test_full_flow(app):
    at = app
    # 1. accounts: wrong password -> clear error; right one -> connected
    at.button(key="test_SRC").click().run()
    assert any("Please fill in" in e.value for e in at.error)          # empty form -> friendly message
    at.text_input(key="demo_SRC_ACCOUNT").set_value("old-acct")
    at.text_input(key="demo_SRC_PASSWORD").set_value("wrong")
    at.button(key="test_SRC").click().run()
    assert any("Incorrect" in e.value for e in at.error)
    at.text_input(key="demo_SRC_PASSWORD").set_value("secret")
    at.button(key="test_SRC").click().run()
    assert any("Connected" in s.value for s in at.success)
    at.text_input(key="demo_TGT_ACCOUNT").set_value("new-acct")
    at.button(key="test_TGT").click().run()
    assert any("Connected" in s.value for s in at.success)
    assert not at.exception

    # 2. backup: scan, deselect HR_DB, back up
    go(at, "2. Back up")
    at.button[0].click().run()                                         # Scan
    ms = at.multiselect[0]
    assert set(ms.value) == {"SALES_DB", "HR_DB"}
    ms.set_value(["SALES_DB"]).run()
    [b for b in at.button if b.label == "Back up now"][0].click().run()
    assert not at.exception, at.exception
    assert any("Backup saved" in s.value for s in at.success)

    # 3. restore
    go(at, "3. Restore")
    at.checkbox[0].check().run()
    [b for b in at.button if b.label == "Restore now"][0].click().run()
    assert any("Restore finished" in s.value for s in at.success)

    # 4. check
    go(at, "4. Check")
    [b for b in at.button if b.label == "Run check"][0].click().run()
    assert any("Everything matches" in s.value for s in at.success)

    # reports
    go(at, "Backups & reports")
    assert any("backup report" in m.value for m in at.markdown)


def test_profiles_roundtrip(app):
    at = app
    at.text_input(key="demo_SRC_ACCOUNT").set_value("old-acct").run()
    at.text_input(key="demo_SRC_PASSWORD").set_value("pw123").run()
    save_box = [t for t in at.text_input if t.label == "Save current details as"][0]
    save_box.set_value("myprofile").run()
    [b for b in at.button if b.label == "Save profile"][0].click().run()
    assert any("Saved" in s.value for s in at.success)
    assert (Path("profiles") / "myprofile.json").exists()
    saved = (Path("profiles") / "myprofile.json").read_text()
    assert "old-acct" in saved and "pw123" not in saved                # passwords never go in a file

    # loading the profile fills the form again
    at.text_input(key="demo_SRC_ACCOUNT").set_value("changed").run()
    at.run()                                                           # profile list refreshes on the next screen update
    [s for s in at.selectbox if s.label == "Load a profile"][0].set_value("myprofile").run()
    [b for b in at.button if b.label == "Load"][0].click().run()
    assert at.text_input(key="demo_SRC_ACCOUNT").value == "old-acct"
