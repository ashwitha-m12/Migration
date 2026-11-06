"""Offline tests for the Azure and Databricks connectors using simulated clients."""
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connectors.azure_connector import AzureConnector          # noqa: E402
from connectors.databricks_connector import DatabricksConnector  # noqa: E402


# ------------------------------------------------------------------ Azure
class Item(dict):
    def __init__(self, name, body): super().__init__(body); self.name = name
    def as_dict(self): return dict(self)


class Ops:
    def __init__(self, items=None): self.items, self.created = list(items or []), []
    def list_by_factory(self, rg, f): return self.items
    def create_or_update(self, rg, f, name, body): self.created.append((name, body)); return body


class FakeADF:
    def __init__(self):
        self.linked_services = Ops([Item("ls_blob", {"name": "ls_blob", "properties": {"type": "AzureBlobStorage",
                                   "typeProperties": {"connectionString": "x", "accountKey": "y"}}})])
        self.datasets, self.data_flows = Ops(), Ops()
        self.pipelines = Ops([Item("pl_load", {"name": "pl_load", "properties": {"activities": []}})])
        self.triggers = Ops([Item("tr_daily", {"name": "tr_daily", "properties": {"type": "ScheduleTrigger"}})])
        self.integration_runtimes = Ops([Item("onprem_ir", {"properties": {"type": "SelfHosted"}})])
        self.factories = NS(get=lambda rg, f: NS(location="westeurope"), create_or_update=lambda *a: None)


class FakeBlob:
    def __init__(self, name, files, hns=False, soft_delete=False):
        self.account_name, self.files, self.hns, self.soft = name, files, hns, soft_delete
        self.uploads = 0

    def get_account_information(self): return {"is_hns_enabled": self.hns, "account_kind": "StorageV2"}
    def get_service_properties(self): return {"delete_retention_policy": NS(enabled=self.soft)}
    def list_containers(self): return [NS(name=c) for c in self.files]

    def get_container_client(self, c):
        outer = self
        import hashlib

        def props(n):
            v = outer.files[c][n]
            return NS(name=n, size=len(v), metadata={},
                      content_settings=NS(content_type="text/csv", content_md5=bytearray(hashlib.md5(v).digest())))

        class BC:
            def __init__(self, n): self.n = n
            def get_blob_properties(self):
                from azure.core.exceptions import ResourceNotFoundError
                if self.n not in outer.files.get(c, {}): raise ResourceNotFoundError("nope")
                return props(self.n)
            def upload_blob(self, f, overwrite, content_settings):
                outer.files.setdefault(c, {})[self.n] = f.read(); outer.uploads += 1

        class CC:
            def list_blobs(self, include=None): return [props(n) for n in outer.files.get(c, {})]
            def download_blob(self, n): return NS(readinto=lambda f: f.write(outer.files[c][n]))
            def create_container(self): outer.files.setdefault(c, {})
            def get_blob_client(self, n): return BC(n)
        return CC()


def _az(side, blob, adf):
    c = AzureConnector(side)
    c.blob, c.adf, c.rg, c.factory, c.only, c.sub, c.cred = blob, adf, "rg1", "adf1", set(), "sub", None
    return c


def test_azure_backup_restore(tmp_path, monkeypatch):
    import connectors.azure_connector as azc
    from connectors.azure_connector import _resource_client
    assert _resource_client.__doc__                                  # the real import path works with this SDK version
    monkeypatch.setattr(azc, "_resource_client", lambda *a: NS(resource_groups=NS(create_or_update=lambda *a: None)))
    src = _az("SRC", FakeBlob("oldstore", {"raw": {"a/b.csv": b"1,2"}}, hns=True), FakeADF())
    res = src.backup(tmp_path)
    assert (tmp_path / "azure/blobs/raw/a/b.csv").read_bytes() == b"1,2"
    tgt_adf = FakeADF()
    tgt = _az("TGT", FakeBlob("newstore", {}, hns=False, soft_delete=True), tgt_adf)
    res = tgt.restore(tmp_path)
    msgs = " | ".join(r.message for r in res)
    assert "used Data Lake" in msgs and "soft delete is ON" in msgs          # warned about both
    assert tgt.blob.uploads == 1
    res2 = tgt.restore(tmp_path)                                             # re-run: identical file is skipped
    assert tgt.blob.uploads == 1 and any("already there" in r.message for r in res2)
    by = {r.kind: r for r in res}
    assert tgt.blob.files["raw"]["a/b.csv"] == b"1,2"
    assert "oldstore.blob.core.windows.net=>newstore.blob.core.windows.net" in by["storage address"].action
    assert "az datafactory trigger start --resource-group rg1 --factory-name adf1 --name tr_daily" in by["adf_triggers"].action
    assert "accountKey" in by["adf_linked_service"].message and "connectionString" in by["adf_linked_service"].message
    assert "list-auth-key" in by["adf_integration_runtime"].action


# ------------------------------------------------------------- Databricks
class Enum(str):
    @property
    def value(self): return str(self)


class FakeWS:
    def __init__(self, user, files=None):
        self.store = files if files is not None else {}
        self.workspace = NS(list=self._list, export=self._export, mkdirs=lambda p: None, import_=self._import)
        self.current_user = NS(me=lambda: NS(user_name=user))
        self.jobs = NS(list=lambda **k: [NS(job_id=7, settings=NS(name="nightly"))],
                       get=lambda i: NS(settings=NS(as_dict=lambda: {"name": "nightly", "schedule": {"quartz_cron_expression": "0 0 1 * * ?"},
                                                                    "tasks": [{"notebook_task": {"notebook_path": f"/Users/{user}/etl"}}]})))
        self.pipelines = NS(list_pipelines=lambda: [])
        self.clusters = NS(list=lambda: [NS(cluster_source=Enum("UI"), cluster_name="dev", cluster_id="c1",
                                            as_dict=lambda: {"cluster_name": "dev", "spark_version": "15.4", "num_workers": 1})])
        self.repos = NS(list=lambda: [NS(path=f"/Repos/{user}/proj", url="https://github.com/x/proj", branch="main")])
        self.secrets = NS(list_scopes=lambda: [NS(name="kv")], list_secrets=lambda s: [NS(key="sf_password")])
        self.posted = []
        self.api_client = NS(do=lambda m, path, body: (self.posted.append((path, body)) or {"job_id": 99}))

    def _list(self, root, recursive=False):
        return [NS(path=f"{root}/etl", object_type=Enum("NOTEBOOK"), language=Enum("PYTHON"))]

    def _export(self, path, format=None):
        return NS(content=base64.b64encode(b"%run /Users/old@x.com/helpers\nprint(1)").decode())

    def _import(self, path, format=None, language=None, content=None, overwrite=False):
        self.store[path] = base64.b64decode(content)


def _dbx(side, w, user):
    c = DatabricksConnector(side)
    c.w, c.me, c.root = w, user, f"/Users/{user}"
    return c


def test_resource_client_import_works():
    from connectors.azure_connector import _resource_client
    class Cred:                                                      # never actually called
        def get_token(self, *a, **k): raise RuntimeError
    assert _resource_client(Cred(), "00000000-0000-0000-0000-000000000000").resource_groups is not None


def test_databricks_backup_restore(tmp_path):
    src = _dbx("SRC", FakeWS("old@x.com"), "old@x.com")
    src.backup(tmp_path)
    tgt_ws = FakeWS("new@y.com")
    res = _dbx("TGT", tgt_ws, "new@y.com").restore(tmp_path)
    assert b"/Users/new@y.com/helpers" in tgt_ws.store["/Users/new@y.com/etl"]      # paths rewritten in code
    job = [b for p, b in tgt_ws.posted if p.endswith("jobs/create")][0]
    assert job["schedule"]["pause_status"] == "PAUSED"
    assert job["tasks"][0]["notebook_task"]["notebook_path"] == "/Users/new@y.com/etl"
    by = {r.kind: r for r in res}
    assert "databricks repos create https://github.com/x/proj --path /Repos/new@y.com/proj" in by["git_folder"].action
    assert "databricks secrets put-secret kv sf_password" in by["secrets"].action
    assert "databricks clusters create --json" in by["cluster"].action


def test_azure_errors_in_plain_language():
    c = AzureConnector("TGT"); c.sub, c.rg, c.factory = "sub-123", "rg1", "adf1"
    e = Exception("(AuthorizationFailed) The client 'cfd89df6-c8da-4ea8-9d7b-5a1bc6e65ed9' with object id 'x' does not "
                  "have authorization to perform action 'Microsoft.Resources/subscriptions/resourcegroups/write' over scope '/subscriptions/sub-123'")
    m = c.explain(e)
    assert "cfd89df6-c8da-4ea8-9d7b-5a1bc6e65ed9" in m and "Contributor" in m and "Privileged administrator roles" in m
    assert "VALUE" in c.explain(Exception("AADSTS7000215: Invalid client secret provided."))
    assert "ONE tenant" in c.explain(Exception("AADSTS700016: Application with identifier 'abc' was not found in the directory"))
    assert "soft delete" in c.explain(Exception("ErrorCode:ContainerBeingDeleted"))
    s = AzureConnector("SRC"); s.sub, s.rg = "sub-1", "rg-old"
    assert "Reader" in s.explain(e)


def test_azure_missing_app_login_is_explained(monkeypatch):
    import connectors.azure_connector as azc
    monkeypatch.setenv("TGT_AZ_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("TGT_AZ_RESOURCE_GROUP", "rg"); monkeypatch.setenv("TGT_AZ_DATA_FACTORY", "f")
    monkeypatch.delenv("TGT_AZ_CLIENT_ID", raising=False)
    monkeypatch.setattr(azc.shutil, "which", lambda n: None)                 # no Azure CLI installed
    c = AzureConnector("TGT")
    try:
        c.connect(); assert False
    except Exception as e:
        assert "App (client) ID" in c.explain(e) and "step 3" in c.explain(e)
    monkeypatch.setenv("TGT_AZ_CLIENT_ID", "cid"); monkeypatch.setenv("TGT_AZ_TENANT_ID", "tid")
    monkeypatch.delenv("TGT_AZ_CLIENT_SECRET", raising=False)
    try:
        c.connect(); assert False
    except Exception as e:
        assert "App client secret" in c.explain(e)
