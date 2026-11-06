"""
Azure -> Azure migration.

  * Blob Storage / ADLS Gen2: every container and file (folder paths preserved)
  * Data Factory: linked services, datasets, data flows, pipelines, triggers

Either part is optional: set the storage connection string, the Data Factory
settings, or both.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path

from core.base import FAILED, MANUAL, OK, SKIPPED, Connector, Field, Friendly, Resource, Result, safe_name

ADF_KINDS = ["linked_services", "datasets", "data_flows", "pipelines", "triggers"]   # dependency order


def _resource_client(cred, sub):
    """azure-mgmt-resource moved this class in version 26; support old and new locations."""
    try:
        from azure.mgmt.resource import ResourceManagementClient
    except ImportError:
        from azure.mgmt.resource.resources import ResourceManagementClient
    return ResourceManagementClient(cred, sub)


def _err(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:220]


class AzureConnector(Connector):
    name = "azure"
    env_prefix = "AZ"
    title = "Azure (Storage + Data Factory)"
    fields = [
        Field("STORAGE_CONNECTION_STRING", "Storage connection string", secret=True,
              help="Portal > Storage accounts > your account > Security + networking > Access keys > key1 > "
                   "Connection string > Show > copy. One connection string = one storage account. "
                   "Leave empty to skip storage."),
        Field("CONTAINERS", "Only these containers (comma separated)", sides=("SRC",), advanced=True),
        Field("SUBSCRIPTION_ID", "Subscription ID (for Data Factory)",
              help="Portal > Subscriptions > Subscription ID. Leave empty to skip Data Factory."),
        Field("TENANT_ID", "Tenant ID",
              help="Portal > Microsoft Entra ID > Overview > Tenant ID (same as your app's 'Directory (tenant) ID')."),
        Field("RESOURCE_GROUP", "Resource group",
              help="Old: Data factories > your factory > Overview > Resource group. New: any name - created if missing."),
        Field("DATA_FACTORY", "Data Factory name",
              help="Old: the factory's name. New: any name - created if missing."),
        Field("CLIENT_ID", "App (client) ID",
              help="Microsoft Entra ID > App registrations > your app > Application (client) ID. "
                   "No app yet? Click 'How do I get these details?' (step 3) - it also explains the role it needs."),
        Field("CLIENT_SECRET", "App client secret", secret=True,
              help="Your app > Certificates & secrets > New client secret > copy the VALUE (shown only once), "
                   "not the Secret ID."),
        Field("LOCATION", "Region for new resources", sides=("TGT",), advanced=True,
              help="For example westeurope. Leave empty to use the same region as the old Data Factory."),
        Field("URL_REWRITE", "Replace storage URLs", sides=("TGT",), advanced=True,
              help="Only if your files moved to a new storage account: "
                   "oldaccount.dfs.core.windows.net=>newaccount.dfs.core.windows.net"),
    ]

    def connect(self):
        self.blob = self.adf = None
        which = "old" if self.side == "SRC" else "new"
        cs = self.env("STORAGE_CONNECTION_STRING")
        if cs:
            from azure.storage.blob import BlobServiceClient
            if "AccountName=" not in cs and "BlobEndpoint=" not in cs:
                raise Friendly(f"The storage connection string for the {which} account doesn't look right. Copy the whole "
                               "'Connection string' under key1 (it starts with DefaultEndpointsProtocol=...), "
                               "not the key on its own.")
            self.blob = BlobServiceClient.from_connection_string(cs)
        self.sub = self.env("SUBSCRIPTION_ID")
        if self.sub:
            from azure.identity import AzureCliCredential, ClientSecretCredential
            from azure.mgmt.datafactory import DataFactoryManagementClient
            if self.env("CLIENT_ID"):
                self.cred = ClientSecretCredential(self.env("TENANT_ID", required=True), self.env("CLIENT_ID"),
                                                   self.env("CLIENT_SECRET", required=True))
            elif shutil.which("az"):
                self.cred = AzureCliCredential(tenant_id=self.env("TENANT_ID"))   # uses `az login`
            else:
                raise Friendly(f"Data Factory needs an app login for the {which} account: fill in 'App (client) ID' and "
                               "'App client secret'. Click 'How do I get these details?' (step 3) to create them. "
                               "(The other option, the Azure CLI, isn't installed on this computer.)")
            self.adf = DataFactoryManagementClient(self.cred, self.sub)
            self.rg = self.env("RESOURCE_GROUP", required=True)
            self.factory = self.env("DATA_FACTORY", required=True)
        if not self.blob and not self.adf:
            raise Friendly(f"Nothing to connect for the {which} account: fill in a Storage connection string, "
                           "the Data Factory details (Subscription ID, Tenant ID, Resource group, Factory name, app login), or both.")
        self.only = set(self.env_list("CONTAINERS"))

    # ---- plain-language errors ---------------------------------------------
    def explain(self, err: Exception) -> str:
        if isinstance(err, Friendly):
            return str(err)
        t = str(err)
        low = t.lower()
        which = "new" if self.side == "TGT" else "old"
        cid = re.search(r"client '([0-9a-fA-F-]{36})'", t) or re.search(r"[Aa]pp(?:lication)? (?:with identifier )?'?([0-9a-fA-F-]{36})", t)
        app = cid.group(1) if cid else "your app (App client ID)"
        if "authorizationfailed" in low or "does not have authorization" in low or "linkedauthorizationfailed" in low:
            return self._role_help(app)
        if "aadsts7000215" in low or "invalid client secret" in low:
            return ("The app client secret is wrong. Paste the secret's VALUE (shown once when created), not its Secret ID. "
                    "Lost it? App registrations > your app > Certificates & secrets > New client secret.")
        if "aadsts700016" in low or ("application with identifier" in low and "not found" in low):
            return (f"App {app} doesn't exist in this tenant. An app registration belongs to ONE tenant: check the Tenant ID, "
                    f"or create an app in the {which} account's tenant (guide step 3).")
        if "aadsts90002" in low or "aadsts900023" in low or ("tenant" in low and "not found" in low):
            return "The Tenant ID is wrong. Copy it from Microsoft Entra ID > Overview > Tenant ID."
        if "subscriptionnotfound" in low or "invalidsubscriptionid" in low:
            return (f"Subscription {getattr(self, 'sub', '')} wasn't found, or the app can't see it. Check the Subscription ID, "
                    "and that the app has a role on it (guide step 4).")
        if "resourcegroupnotfound" in low:
            return (f"Resource group '{getattr(self, 'rg', '')}' doesn't exist in this subscription. Check the name "
                    "(Data factories > your factory > Overview > Resource group).")
        if "resourcenotfound" in low and "factor" in low:
            return f"Data Factory '{getattr(self, 'factory', '')}' wasn't found in resource group '{getattr(self, 'rg', '')}'. Check the name."
        if "containerbeingdeleted" in low:
            return ("A deleted container with this name is still being kept by soft delete in the target storage account. "
                    "Wait until its retention period ends, or restore into a new storage account with soft delete off.")
        if "authenticationfailed" in low or "server failed to authenticate" in low:
            return (f"The {which} storage account rejected the key. Copy the connection string again "
                    "(Access keys > key1 > Connection string); keys change when they're rotated.")
        if "getaddrinfo" in low or "name or service not known" in low or "failed to resolve" in low:
            return f"Can't reach the {which} storage account. Check the account name in the connection string and your network."
        if "is not recognized" in low and "az" in low or "azure cli not found" in low:
            return "Fill in 'App (client) ID' and 'App client secret' (guide step 3); the Azure CLI isn't installed here."
        return super().explain(err)

    def _role_help(self, app: str) -> str:
        if self.side == "TGT":
            return (f"App {app} signed in, but has no permission in the NEW account. Give it the Contributor role on the "
                    f"subscription {getattr(self, 'sub', '')}: Portal > Subscriptions > open it > Access control (IAM) > "
                    "Add > Add role assignment > 'Privileged administrator roles' tab > Contributor > Next > "
                    "Select members > your app > Review + assign. Wait 1-2 minutes, then test again.")
        return (f"App {app} signed in, but can't read the OLD Data Factory. Give it the Reader role on resource group "
                f"'{getattr(self, 'rg', '')}': open it > Access control (IAM) > Add role assignment > Reader > "
                "your app > Review + assign. Wait 1-2 minutes, then test again.")

    # ---- checks run by "Test connection" ------------------------------------
    def _account_info(self) -> dict:
        info = {}
        try:
            a = self.blob.get_account_information()
            info["hns"] = bool(a.get("is_hns_enabled"))
            info["kind"] = a.get("account_kind")
        except Exception:
            pass
        try:
            props = self.blob.get_service_properties()
            pol = props.get("delete_retention_policy")
            info["soft_delete"] = bool(getattr(pol, "enabled", False) or (isinstance(pol, dict) and pol.get("enabled")))
        except Exception:
            pass
        return info

    def _permissions_ok(self) -> str | None:
        """Ask Azure which actions the app may do. Returns a fix message if it can't do what this side needs."""
        try:
            import requests
            token = self.cred.get_token("https://management.azure.com/.default").token
        except Exception as e:
            raise Friendly(self.explain(e))
        h = {"Authorization": f"Bearer {token}"}
        base = f"https://management.azure.com/subscriptions/{self.sub}"
        for scope in (f"{base}/resourceGroups/{self.rg}", base):
            try:
                r = requests.get(f"{scope}/providers/Microsoft.Authorization/permissions",
                                 params={"api-version": "2022-04-01"}, headers=h, timeout=20)
            except Exception:
                return None                                   # can't check -> don't block
            if r.status_code == 404:
                continue                                      # resource group doesn't exist yet -> check subscription
            if r.status_code != 200:
                return None
            actions = {a.lower() for p in r.json().get("value", []) for a in p.get("actions", [])}
            if not actions:
                return self._role_help("your app (App client ID)")
            if self.side == "SRC":
                ok = "*" in actions or "*/read" in actions or any(a.startswith("microsoft.datafactory/") for a in actions)
            else:
                ok = "*" in actions or ("microsoft.datafactory/*" in actions or "microsoft.datafactory/factories/*" in actions)
            return None if ok else self._role_help("your app (App client ID)")
        return None

    def preflight(self) -> str:
        notes = []
        if self.blob:
            info = self._account_info()
            dl = {True: "yes", False: "no"}.get(info.get("hns"), "unknown")
            notes.append(f"storage account '{self.blob.account_name}' (Data Lake: {dl})")
            if self.side == "TGT" and info.get("soft_delete"):
                notes.append("note: soft delete is ON here - fine for one restore, but re-runs keep hidden old copies")
        if self.adf:
            problem = self._permissions_ok()
            if problem:
                raise Friendly(problem)
            notes.append(f"Data Factory '{self.factory}' permissions OK")
        return "; ".join(notes)

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
                        cs_ = b.content_settings
                        md5 = base64.b64encode(bytes(cs_.content_md5)).decode() if cs_ and cs_.content_md5 else None
                        index[c].append({"name": b.name, "file": rel.as_posix(), "size": b.size, "md5": md5,
                                         "content_type": cs_.content_type if cs_ else None})
                        n += 1
                    R.append(Result("container", c, OK, f"{n} file(s) downloaded"))
                except Exception as e:
                    R.append(Result("container", c, FAILED, self.explain(e)))
            (base / "blobs.json").write_text(json.dumps(index, indent=2))
            info = self._account_info()
            (base / "storage_meta.json").write_text(json.dumps({"account": self.blob.account_name, "hns": info.get("hns")}))
            R.append(Result("storage account", self.blob.account_name, OK,
                            f"{len(index)} container(s) backed up (Data Lake: "
                            f"{ {True: 'yes', False: 'no'}.get(info.get('hns'), 'unknown') }). Containers in OTHER storage "
                            "accounts aren't included - back each one up with its own connection string."))
        if self.adf:
            try:
                fac = self.adf.factories.get(self.rg, self.factory)
            except Exception as e:
                R.append(Result("adf_factory", self.factory, FAILED, self.explain(e)))
                return R
            (base / "adf").mkdir(exist_ok=True)
            (base / "adf" / "factory.json").write_text(json.dumps({"location": fac.location, "name": self.factory}))
            for kind in ADF_KINDS:
                d = base / "adf" / kind
                d.mkdir(parents=True, exist_ok=True)
                for item in self._adf_items(kind):
                    (d / f"{safe_name(item.name)}.json").write_text(json.dumps(item.as_dict(), indent=2, default=str))
                    if kind == "linked_services":
                        R.append(Result("adf_linked_service", item.name, OK,
                                        "exported (Azure never returns passwords/keys - restore lists what to re-enter)"))
                    else:
                        R.append(Result(f"adf_{kind[:-1]}", item.name, OK, "definition exported"))
            self_hosted = []
            for ir in self.adf.integration_runtimes.list_by_factory(self.rg, self.factory):
                t = ir.as_dict().get("properties", {}).get("type")
                if t == "SelfHosted":
                    self_hosted.append(ir.name)
                    R.append(Result("adf_integration_runtime", ir.name, OK, "self-hosted runtime saved - restore gives the setup commands"))
            (base / "adf" / "self_hosted_runtimes.json").write_text(json.dumps(self_hosted))
        return R

    # ----------------------------------------------------------------- restore
    def restore(self, pkg: Path) -> list[Result]:
        base, R = pkg / "azure", []
        index_file = base / "blobs.json"
        if index_file.exists():
            if not self.blob:
                R.append(Result("storage", "blobs", SKIPPED, "no Storage connection string given for the new account"))
            else:
                R += self._restore_blobs(base, json.loads(index_file.read_text()))

        adf_dir = base / "adf"
        if adf_dir.exists():
            if not self.adf:
                R.append(Result("adf", "data factory", SKIPPED, f"{self.var('SUBSCRIPTION_ID')} not set"))
                return R
            R += self._restore_adf(adf_dir)
        return R

    def _restore_blobs(self, base: Path, index: dict) -> list[Result]:
        from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
        from azure.storage.blob import ContentSettings
        R = []
        meta = json.loads((base / "storage_meta.json").read_text()) if (base / "storage_meta.json").exists() else {}
        info = self._account_info()
        if meta.get("hns") and info.get("hns") is False:
            R.append(Result("storage account", self.blob.account_name, MANUAL,
                            f"the OLD account '{meta.get('account')}' used Data Lake (hierarchical namespace) but this one "
                            "doesn't. Files are copied, but pipelines and linked services using '.dfs.core.windows.net' "
                            "addresses will fail here. Recommended: create a new storage account with 'Enable "
                            "hierarchical namespace' ticked (Advanced tab) and soft delete off, then restore into it."))
        if info.get("soft_delete"):
            R.append(Result("storage account", self.blob.account_name, MANUAL,
                            "soft delete is ON. Unchanged files are skipped on re-runs, but any file that is overwritten "
                            "leaves a hidden old copy, which blocks a later Data Lake upgrade. To turn it off: storage "
                            "account > Data management > Data protection > untick soft delete for blobs and containers."))
        for c, items in index.items():
            cc = self.blob.get_container_client(c)
            try:
                try:
                    cc.create_container()
                except ResourceExistsError:
                    pass
                up = same = 0
                for it in items:
                    bc = cc.get_blob_client(it["name"])
                    try:
                        props = bc.get_blob_properties()
                    except ResourceNotFoundError:
                        props = None
                    if props is not None and it.get("md5") and props.size == it.get("size"):
                        cur = props.content_settings.content_md5 if props.content_settings else None
                        if cur and base64.b64encode(bytes(cur)).decode() == it["md5"]:
                            same += 1                 # identical file already there - don't overwrite
                            continue
                    with open(base / it["file"], "rb") as f:
                        bc.upload_blob(f, overwrite=True,
                                       content_settings=ContentSettings(content_type=it.get("content_type")))
                    up += 1
                msg = f"{up} file(s) uploaded" + (f", {same} already there (unchanged, skipped)" if same else "")
                R.append(Result("container", c, OK, msg))
            except Exception as e:
                R.append(Result("container", c, FAILED, self.explain(e)))
        meta_old = meta.get("account")
        new_acct = self.blob.account_name
        if meta_old and meta_old != new_acct:
            R.append(Result("storage address", f"{meta_old} -> {new_acct}", MANUAL,
                            "your files now live in a different storage account. Anything pointing at the old one "
                            "(Data Factory linked services, Snowflake stages, apps) must be updated. Put this in "
                            "'Replace storage URLs' (New account > Advanced) before restoring Data Factory or Snowflake:",
                            f"{meta_old}.blob.core.windows.net=>{new_acct}.blob.core.windows.net,"
                            f"{meta_old}.dfs.core.windows.net=>{new_acct}.dfs.core.windows.net"))
        return R

    def _restore_adf(self, adf_dir: Path) -> list[Result]:
        R = []
        src = json.loads((adf_dir / "factory.json").read_text())
        loc = self.env("LOCATION") or src.get("location") or "eastus"
        try:
            _resource_client(self.cred, self.sub).resource_groups.create_or_update(self.rg, {"location": loc})
            try:
                self.adf.factories.get(self.rg, self.factory)
            except Exception:
                self.adf.factories.create_or_update(self.rg, self.factory, {"location": loc})
                R.append(Result("adf_factory", self.factory, OK, f"created in {loc}"))
        except Exception as e:
            R.append(Result("adf_factory", self.factory, FAILED, self.explain(e)))
            return R

        started_off = []
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
                            secret_fields = sorted({k for k in json.dumps(body.get("properties", {}).get("typeProperties", {})).split('"')
                                                    if re.search(r"(?i)password|key|secret|connectionstring|sasuri|token", k)})
                            lstype = body.get("properties", {}).get("type", "")
                            R.append(Result("adf_linked_service", name, MANUAL,
                                            f"created ({lstype}). Re-enter: {', '.join(secret_fields) or 'its credentials'} - "
                                            f"ADF Studio > Manage > Linked services > {name} > edit > Test connection."))
                        elif kind == "triggers":
                            started_off.append(name)
                            R.append(Result("adf_trigger", name, OK, "created (stopped)"))
                        else:
                            R.append(Result(f"adf_{kind[:-1]}", name, OK, "created"))
                    except Exception as e:
                        failed.append((f, name, e))
                if not failed or len(failed) == len(todo):
                    break
                todo = [f for f, _, _ in failed]
            for _, name, e in failed:
                R.append(Result(f"adf_{kind[:-1]}", name, FAILED, self.explain(e)))
        if started_off:
            R.append(Result("adf_triggers", f"{len(started_off)} trigger(s)", MANUAL,
                            "created stopped, so nothing runs before you're ready. To start them:",
                            "\n".join(f"az datafactory trigger start --resource-group {self.rg} --factory-name {self.factory} --name {t}"
                                      for t in started_off)))
        shir = adf_dir / "self_hosted_runtimes.json"
        for ir in (json.loads(shir.read_text()) if shir.exists() else []):
            R.append(Result("adf_integration_runtime", ir, MANUAL,
                            "self-hosted runtime: create it, then register your on-premises agent with the key it prints:",
                            f"az datafactory integration-runtime self-hosted create --resource-group {self.rg} --factory-name {self.factory} --name {ir}\n"
                            f"az datafactory integration-runtime list-auth-key --resource-group {self.rg} --factory-name {self.factory} --name {ir}"))
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
                    R.append(Result("container", c, FAILED, self.explain(e)))
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
