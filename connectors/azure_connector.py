"""
Azure -> Azure migration.

  * Blob Storage / ADLS Gen2: every container and file (folder paths preserved)
  * Data Factory: linked services, datasets, data flows, pipelines, triggers

Either part is optional: set the storage connection string, the Data Factory
settings, or both.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.base import FAILED, MANUAL, OK, SKIPPED, Connector, Field, Resource, Result, safe_name

ADF_KINDS = ["linked_services", "datasets", "data_flows", "pipelines", "triggers"]   # dependency order


def _err(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:220]


class AzureConnector(Connector):
    name = "azure"
    env_prefix = "AZ"
    title = "Azure (Storage + Data Factory)"
    fields = [
        Field("STORAGE_CONNECTION_STRING", "Storage connection string", secret=True,
              help="Portal > Storage account > Access keys > Connection string"),
        Field("CONTAINERS", "Only these containers (comma separated)", sides=("SRC",), advanced=True),
        Field("SUBSCRIPTION_ID", "Subscription ID (for Data Factory)"),
        Field("TENANT_ID", "Tenant ID"),
        Field("RESOURCE_GROUP", "Resource group"),
        Field("DATA_FACTORY", "Data Factory name"),
        Field("LOCATION", "Region for new resources", default="centralindia", sides=("TGT",)),
        Field("CLIENT_ID", "Service principal client ID", advanced=True, help="Leave empty to use `az login`"),
        Field("CLIENT_SECRET", "Service principal secret", secret=True, advanced=True),
        Field("URL_REWRITE", "Replace storage URLs", sides=("TGT",), advanced=True),
    ]

    def connect(self):
        self.blob = self.adf = None
        cs = self.env("STORAGE_CONNECTION_STRING")
        if cs:
            from azure.storage.blob import BlobServiceClient
            self.blob = BlobServiceClient.from_connection_string(cs)
        self.sub = self.env("SUBSCRIPTION_ID")
        if self.sub:
            from azure.identity import AzureCliCredential, ClientSecretCredential
            from azure.mgmt.datafactory import DataFactoryManagementClient
            if self.env("CLIENT_ID"):
                self.cred = ClientSecretCredential(self.env("TENANT_ID", required=True), self.env("CLIENT_ID"),
                                                   self.env("CLIENT_SECRET", required=True))
            else:
                self.cred = AzureCliCredential(tenant_id=self.env("TENANT_ID"))   # uses `az login`
            self.adf = DataFactoryManagementClient(self.cred, self.sub)
            self.rg = self.env("RESOURCE_GROUP", required=True)
            self.factory = self.env("DATA_FACTORY", required=True)
        if not self.blob and not self.adf:
            raise RuntimeError(f"Set {self.var('STORAGE_CONNECTION_STRING')} and/or {self.var('SUBSCRIPTION_ID')} in .env")
        self.only = set(self.env_list("CONTAINERS"))

    def _containers(self) -> list[str]:
        names = [c.name for c in self.blob.list_containers() if not c.name.startswith("$")]
        return [n for n in names if not self.only or n in self.only]

    def _files(self, container: str):
        cc = self.blob.get_container_client(container)
        for b in cc.list_blobs(include=["metadata"]):
            if (b.metadata or {}).get("hdi_isfolder") == "true" or (b.name.endswith("/") and not b.size):
                continue                                  # ADLS folder markers
            yield cc, b

    def _adf_items(self, kind: str):
        return list(getattr(self.adf, kind).list_by_factory(self.rg, self.factory))

    # ---------------------------------------------------------------- discover
    def discover(self) -> list[Resource]:
        res = []
        if self.blob:
            for c in self._containers():
                n = size = 0
                for _, b in self._files(c):
                    n, size = n + 1, size + (b.size or 0)
                res.append(Resource("container", c, {"files": n, "mb": round(size / 1e6, 2)}))
        if self.adf:
            for kind in ADF_KINDS:
                for item in self._adf_items(kind):
                    res.append(Resource(f"adf_{kind[:-1]}", item.name))
        return res

    # ------------------------------------------------------------------ backup
    def backup(self, pkg: Path) -> list[Result]:
        base, R = pkg / "azure", []
        base.mkdir(parents=True, exist_ok=True)
        if self.blob:
            index = {}
            for c in self._containers():
                index[c], n = [], 0
                try:
                    for cc, b in self._files(c):
                        parts = [safe_name(p) for p in b.name.split("/") if p not in ("", ".", "..")]
                        rel = Path("blobs", safe_name(c), *parts)
                        (base / rel).parent.mkdir(parents=True, exist_ok=True)
                        with open(base / rel, "wb") as f:
                            cc.download_blob(b.name).readinto(f)
                        ctype = b.content_settings.content_type if b.content_settings else None
                        index[c].append({"name": b.name, "file": rel.as_posix(), "content_type": ctype})
                        n += 1
                    R.append(Result("container", c, OK, f"{n} file(s) downloaded"))
                except Exception as e:
                    R.append(Result("container", c, FAILED, _err(e)))
            (base / "blobs.json").write_text(json.dumps(index, indent=2))
            R.append(Result("storage keys", "account keys / SAS", MANUAL,
                            "New storage account = new URL and keys. Update Snowflake stages/integrations "
                            "(TGT_SF_URL_REWRITE) and any app config."))
        if self.adf:
            fac = self.adf.factories.get(self.rg, self.factory)
            (base / "adf").mkdir(exist_ok=True)
            (base / "adf" / "factory.json").write_text(json.dumps({"location": fac.location, "name": self.factory}))
            for kind in ADF_KINDS:
                d = base / "adf" / kind
                d.mkdir(parents=True, exist_ok=True)
                for item in self._adf_items(kind):
                    (d / f"{safe_name(item.name)}.json").write_text(json.dumps(item.as_dict(), indent=2, default=str))
                    if kind == "linked_services":
                        R.append(Result("adf_linked_service", item.name, MANUAL,
                                        "exported, but Azure never returns passwords/keys - re-enter them after restore"))
                    else:
                        R.append(Result(f"adf_{kind[:-1]}", item.name, OK, "definition exported"))
            for ir in self.adf.integration_runtimes.list_by_factory(self.rg, self.factory):
                t = ir.as_dict().get("properties", {}).get("type")
                if t == "SelfHosted":
                    R.append(Result("adf_integration_runtime", ir.name, MANUAL,
                                    "self-hosted runtime - create it in the new factory and register the agent again"))
        return R

    # ----------------------------------------------------------------- restore
    def restore(self, pkg: Path) -> list[Result]:
        base, R = pkg / "azure", []
        index_file = base / "blobs.json"
        if index_file.exists():
            if not self.blob:
                R.append(Result("storage", "blobs", SKIPPED, f"{self.var('STORAGE_CONNECTION_STRING')} not set"))
            else:
                from azure.core.exceptions import ResourceExistsError
                from azure.storage.blob import ContentSettings
                for c, items in json.loads(index_file.read_text()).items():
                    cc = self.blob.get_container_client(c)
                    try:
                        try:
                            cc.create_container()
                        except ResourceExistsError:
                            pass
                        for it in items:
                            with open(base / it["file"], "rb") as f:
                                cc.upload_blob(it["name"], f, overwrite=True,
                                               content_settings=ContentSettings(content_type=it.get("content_type")))
                        R.append(Result("container", c, OK, f"{len(items)} file(s) uploaded"))
                    except Exception as e:
                        R.append(Result("container", c, FAILED, _err(e)))

        adf_dir = base / "adf"
        if adf_dir.exists():
            if not self.adf:
                R.append(Result("adf", "data factory", SKIPPED, f"{self.var('SUBSCRIPTION_ID')} not set"))
                return R
            R += self._restore_adf(adf_dir)
        return R

    def _restore_adf(self, adf_dir: Path) -> list[Result]:
        from azure.mgmt.resource import ResourceManagementClient
        R = []
        src = json.loads((adf_dir / "factory.json").read_text())
        loc = self.env("LOCATION") or src.get("location") or "eastus"
        ResourceManagementClient(self.cred, self.sub).resource_groups.create_or_update(self.rg, {"location": loc})
        try:
            self.adf.factories.get(self.rg, self.factory)
        except Exception:
            self.adf.factories.create_or_update(self.rg, self.factory, {"location": loc})
            R.append(Result("adf_factory", self.factory, OK, f"created in {loc}"))

        for kind in ADF_KINDS:
            todo = sorted((adf_dir / kind).glob("*.json"))
            for _ in range(4):                  # retry: pipelines can call other pipelines, etc.
                failed = []
                for f in todo:
                    body = json.loads(self.apply_rewrites(f.read_text()))
                    name = body.get("name") or f.stem
                    for k in ("id", "etag", "type", "name"):
                        body.pop(k, None)
                    try:
                        getattr(self.adf, kind).create_or_update(self.rg, self.factory, name, body)
                        if kind == "linked_services":
                            R.append(Result("adf_linked_service", name, MANUAL, "created - re-enter its password/key/SAS"))
                        elif kind == "triggers":
                            R.append(Result("adf_trigger", name, MANUAL, "created STOPPED - start it in ADF Studio when ready"))
                        else:
                            R.append(Result(f"adf_{kind[:-1]}", name, OK, "created"))
                    except Exception as e:
                        failed.append((f, name, e))
                if not failed or len(failed) == len(todo):
                    break
                todo = [f for f, _, _ in failed]
            for _, name, e in failed:
                R.append(Result(f"adf_{kind[:-1]}", name, FAILED, _err(e)))
        return R

    # ---------------------------------------------------------------- validate
    def validate(self, pkg: Path) -> list[Result]:
        base, R = pkg / "azure", []
        index_file = base / "blobs.json"
        if index_file.exists() and self.blob:
            for c, items in json.loads(index_file.read_text()).items():
                try:
                    new = {b.name for _, b in self._files(c)}
                    missing = [i["name"] for i in items if i["name"] not in new]
                    R.append(Result("container", c, FAILED if missing else OK,
                                    f"{len(missing)} missing, e.g. {missing[:3]}" if missing else f"{len(items)} files present"))
                except Exception as e:
                    R.append(Result("container", c, FAILED, _err(e)))
        if (base / "adf").exists() and self.adf:
            for kind in ADF_KINDS:
                old = {json.loads(f.read_text()).get("name") for f in (base / "adf" / kind).glob("*.json")}
                if not old:
                    continue
                new = {i.name for i in self._adf_items(kind)}
                miss = sorted(old - new)
                R.append(Result(f"adf_{kind}", f"{len(old)} item(s)", FAILED if miss else OK,
                                f"missing: {miss}" if miss else "all present"))
        return R
