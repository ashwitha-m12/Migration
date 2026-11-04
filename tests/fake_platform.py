"""A pretend platform used to test the app without a real cloud account."""
import json
import os
import tempfile
from pathlib import Path

from core.base import FAILED, MANUAL, OK, Connector, Field, Resource, Result

STORE = Path(tempfile.gettempdir()) / "cmt_fake_store.json"


def _db():
    return json.loads(STORE.read_text()) if STORE.exists() else {
        "SRC": {"SALES_DB": {"ORDERS": 7, "CUSTOMERS": 5}, "HR_DB": {"EMP": 3}}, "TGT": {}}


class FakeConnector(Connector):
    name, env_prefix, title = "demo", "DEMO", "Demo platform (for testing)"
    fields = [Field("ACCOUNT", "Account", required=True), Field("PASSWORD", "Password", secret=True),
              Field("REGION", "Region", default="central-india", advanced=True)]

    def connect(self):
        if self.env("PASSWORD") == "wrong":
            raise RuntimeError("Incorrect username or password was specified.")
        self.env("ACCOUNT", required=True)

    def discover(self):
        d = _db()[self.side]
        only = set(self.env_list("DATABASES"))
        return [Resource("database", n) for n in d if not only or n in only] + \
               [Resource("table", f"{n}.{t}") for n, ts in d.items() for t in ts if not only or n in only]

    def backup(self, pkg):
        d = {k: v for k, v in _db()["SRC"].items() if not self.env_list("DATABASES") or k in self.env_list("DATABASES")}
        (pkg / "demo").mkdir()
        (pkg / "demo" / "data.json").write_text(json.dumps(d))
        for n in d:
            self.log(f"Saving {n} ...")
        return [Result("database", n, OK, f"{len(t)} tables") for n, t in d.items()] + \
               [Result("tasks", "pipeline", MANUAL, "resume tasks after restore")]

    def restore(self, pkg):
        db = _db()
        db["TGT"] = json.loads((pkg / "demo" / "data.json").read_text())
        STORE.write_text(json.dumps(db))
        self.log("Restored.")
        return [Result("database", n, OK, "created") for n in db["TGT"]]

    def validate(self, pkg):
        want = json.loads((pkg / "demo" / "data.json").read_text())
        have = _db()["TGT"]
        return [Result("database", n, OK if have.get(n) == t else FAILED, "rows match" if have.get(n) == t else "differs")
                for n, t in want.items()]
