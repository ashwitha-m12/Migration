"""
Databricks -> Databricks migration.

  * Notebooks and files under your workspace folder (folder tree preserved;
    /Users/old@mail -> /Users/new@mail paths fixed automatically, including %run paths)
  * Jobs / workflows (schedules are created PAUSED)
  * Lakeflow / DLT pipelines
  * Cluster configs saved for reference
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from core.base import FAILED, MANUAL, OK, Connector, Field, Resource, Result, safe_name


def _err(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:220]


def _pause(settings: dict) -> bool:
    paused = False
    for key in ("schedule", "trigger", "continuous"):
        if isinstance(settings.get(key), dict):
            settings[key]["pause_status"] = "PAUSED"
            paused = True
    return paused


def _swap_cluster(obj, cluster_id: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "existing_cluster_id":
                obj[k] = cluster_id
            else:
                _swap_cluster(v, cluster_id)
    elif isinstance(obj, list):
        for v in obj:
            _swap_cluster(v, cluster_id)


class DatabricksConnector(Connector):
    name = "databricks"
    env_prefix = "DBX"
    title = "Databricks"
    fields = [
        Field("HOST", "Workspace URL", required=True, help="https://adb-....azuredatabricks.net"),
        Field("TOKEN", "Access token", secret=True, required=True, help="Settings > Developer > Access tokens"),
        Field("ROOT", "Folder to migrate", advanced=True, help="Default: your home folder"),
        Field("EXISTING_CLUSTER_ID", "Cluster ID for jobs", sides=("TGT",), advanced=True),
    ]

    def connect(self):
        from databricks.sdk import WorkspaceClient
        self.w = WorkspaceClient(host=self.env("HOST", required=True), token=self.env("TOKEN", required=True))
        self.me = self.w.current_user.me().user_name
        self.root = (self.env("ROOT") or f"/Users/{self.me}").rstrip("/")

    def explain(self, err: Exception) -> str:
        low = str(err).lower()
        which = "old" if self.side == "SRC" else "new"
        if "401" in low or "invalid access token" in low or "unauthorized" in low:
            return (f"The {which} workspace rejected the access token. Create a new one: your name (top right) > Settings > "
                    "Developer > Access tokens > Generate new token, and paste it.")
        if "403" in low or "permission_denied" in low:
            return f"The token's user lacks permission in the {which} workspace. Use a workspace admin's token."
        if "resolve" in low or "name or service not known" in low or "getaddrinfo" in low:
            return f"Can't reach the {which} workspace. Check the Workspace URL (https://adb-....azuredatabricks.net)."
        return super().explain(err)

    def _objects(self):
        return list(self.w.workspace.list(self.root, recursive=True))

    def discover(self) -> list[Resource]:
        res = [Resource(o.object_type.value.lower(), o.path) for o in self._objects()
               if o.object_type.value in ("NOTEBOOK", "FILE", "REPO")]
        res += [Resource("job", j.settings.name if j.settings else str(j.job_id)) for j in self.w.jobs.list()]
        res += [Resource("pipeline", p.name) for p in self.w.pipelines.list_pipelines()]
        return res

    # ------------------------------------------------------------------ backup
    def backup(self, pkg: Path) -> list[Result]:
        from databricks.sdk.service.workspace import ExportFormat
        base, R, index = pkg / "databricks", [], []
        repo_paths = []
        for o in self._objects():
            t = o.object_type.value
            if t == "REPO":
                repo_paths.append(o.path)
                continue
            if t not in ("NOTEBOOK", "FILE"):
                continue
            rel = o.path[len(self.root):].lstrip("/")
            local = Path("workspace", *[safe_name(p) for p in rel.split("/")])
            try:
                content = self.w.workspace.export(o.path, format=ExportFormat.SOURCE).content
                (base / local).parent.mkdir(parents=True, exist_ok=True)
                (base / local).write_bytes(base64.b64decode(content or ""))
                index.append({"rel": rel, "type": t, "file": local.as_posix(),
                              "language": o.language.value if o.language else None})
                R.append(Result(t.lower(), rel, OK, "exported"))
            except Exception as e:
                R.append(Result(t.lower(), rel, FAILED, _err(e)))
        base.mkdir(parents=True, exist_ok=True)
        (base / "workspace.json").write_text(json.dumps({"root": self.root, "user": self.me, "objects": index}, indent=2))

        (base / "jobs").mkdir(exist_ok=True)
        for j in self.w.jobs.list():
            try:
                settings = self.w.jobs.get(j.job_id).settings.as_dict()
                (base / "jobs" / f"{j.job_id}.json").write_text(json.dumps(settings, indent=2))
                note = "uses an existing cluster id - set TGT_DBX_EXISTING_CLUSTER_ID" if "existing_cluster_id" in json.dumps(settings) else "exported"
                R.append(Result("job", settings.get("name", str(j.job_id)), OK, note))
            except Exception as e:
                R.append(Result("job", str(j.job_id), FAILED, _err(e)))

        (base / "pipelines").mkdir(exist_ok=True)
        for p in self.w.pipelines.list_pipelines():
            try:
                spec = self.w.pipelines.get(p.pipeline_id).spec.as_dict()
                (base / "pipelines" / f"{safe_name(p.name)}.json").write_text(json.dumps(spec, indent=2))
                R.append(Result("pipeline", p.name, OK, "exported"))
            except Exception as e:
                R.append(Result("pipeline", p.name, FAILED, _err(e)))

        (base / "clusters").mkdir(exist_ok=True)
        for c in self.w.clusters.list():
            src = c.cluster_source.value if c.cluster_source else ""
            if src in ("UI", "API"):
                keep = {k: v for k, v in c.as_dict().items() if k in (
                    "cluster_name", "spark_version", "node_type_id", "num_workers", "autoscale",
                    "autotermination_minutes", "spark_conf", "spark_env_vars", "data_security_mode", "single_user_name")}
                (base / "clusters" / f"{safe_name(c.cluster_name or c.cluster_id)}.json").write_text(json.dumps(keep, indent=2))
                R.append(Result("cluster", c.cluster_name, OK, "settings saved - restore gives the create command"))

        # Git folders, secret scopes: saved so restore can give exact commands
        repos = []
        try:
            for r in self.w.repos.list():
                rp = r.path or ""
                if rp in repo_paths or rp.startswith(self.root + "/") or \
                        rp.startswith(self.root.replace("/Users/", "/Repos/", 1) + "/"):   # Git folders live in either place
                    repos.append({"path": r.path, "url": r.url, "branch": r.branch})
        except Exception:
            repos = [{"path": pth, "url": "", "branch": ""} for pth in repo_paths]
        scopes = []
        try:
            for sc in self.w.secrets.list_scopes():
                keys = [k.key for k in self.w.secrets.list_secrets(sc.name)]
                scopes.append({"scope": sc.name, "keys": keys})
        except Exception:
            pass
        (base / "followups.json").write_text(json.dumps({"repos": repos, "secret_scopes": scopes}, indent=2))
        if repos:
            R.append(Result("git_folders", f"{len(repos)} Git folder(s)", OK, "saved - restore gives the re-clone commands"))
        if scopes:
            R.append(Result("secrets", f"{len(scopes)} secret scope(s)", OK, "names saved (values can't be read) - restore gives the commands"))

        R.append(Result("tables", "Delta / Unity Catalog data", MANUAL,
                        "Table data isn't copied by this version. Before the trial ends, write important tables to "
                        "your own storage (e.g. df.write.format('delta').save('abfss://...')) or back them up "
                        "through Snowflake/Azure."))
        return R

    # ----------------------------------------------------------------- restore
    def restore(self, pkg: Path) -> list[Result]:
        from databricks.sdk.service.workspace import ImportFormat, Language
        base, R = pkg / "databricks", []
        ws = json.loads((base / "workspace.json").read_text())
        old_root = ws["root"]
        made = set()
        for it in ws["objects"]:
            path = f"{self.root}/{it['rel']}"
            parent = path.rsplit("/", 1)[0]
            try:
                if parent not in made:
                    self.w.workspace.mkdirs(parent)
                    made.add(parent)
                raw = (base / it["file"]).read_bytes()
                try:     # fix absolute paths like %run /Users/old@mail/... inside the code
                    raw = self.apply_rewrites(raw.decode("utf-8").replace(old_root, self.root)).encode("utf-8")
                except UnicodeDecodeError:
                    pass
                content = base64.b64encode(raw).decode()
                if it["type"] == "NOTEBOOK":
                    self.w.workspace.import_(path=path, format=ImportFormat.SOURCE,
                                             language=Language(it["language"]), content=content, overwrite=True)
                else:
                    self.w.workspace.import_(path=path, format=ImportFormat.AUTO, content=content, overwrite=True)
                R.append(Result(it["type"].lower(), it["rel"], OK, "imported"))
            except Exception as e:
                R.append(Result(it["type"].lower(), it["rel"], FAILED, _err(e)))

        cluster = self.env("EXISTING_CLUSTER_ID")
        for f in sorted((base / "jobs").glob("*.json")):
            settings = json.loads(self.apply_rewrites(f.read_text().replace(old_root, self.root)))
            settings.pop("run_as", None)
            if cluster:
                _swap_cluster(settings, cluster)
            paused = _pause(settings)
            name = settings.get("name", f.stem)
            try:
                resp = self.w.api_client.do("POST", "/api/2.1/jobs/create", body=settings)
                if paused:
                    R.append(Result("job", name, MANUAL,
                                    f"created (id {resp.get('job_id')}) with its schedule PAUSED. To start it: "
                                    f"Workflows > {name} > Schedules & triggers > Unpause."))
                else:
                    R.append(Result("job", name, OK, f"created (id {resp.get('job_id')})"))
            except Exception as e:
                R.append(Result("job", name, FAILED, _err(e)))

        # follow-ups with exact commands
        fu = json.loads((base / "followups.json").read_text()) if (base / "followups.json").exists() else {}
        for repo in fu.get("repos", []):
            path = repo["path"].replace(old_root, self.root)
            if old_root.startswith("/Users/"):
                path = path.replace(old_root.replace("/Users/", "/Repos/"), self.root.replace("/Users/", "/Repos/"))
            R.append(Result("git_folder", path, MANUAL,
                            "Git folder - re-clone it (Databricks CLI):",
                            f"databricks repos create {repo['url'] or '<repo url>'} --path {path}"
                            + (f"\ndatabricks repos update {path} --branch {repo['branch']}" if repo.get("branch") else "")))
        if fu.get("secret_scopes"):
            lines = []
            for sc in fu["secret_scopes"]:
                lines.append(f"databricks secrets create-scope {sc['scope']}")
                lines += [f"databricks secrets put-secret {sc['scope']} {k}" for k in sc["keys"]]
            R.append(Result("secrets", f"{len(fu['secret_scopes'])} secret scope(s)", MANUAL,
                            "values can't be copied. Create the scopes and type each value when asked:", "\n".join(lines)))
        for cf in sorted((base / "clusters").glob("*.json")) if (base / "clusters").exists() else []:
            R.append(Result("cluster", cf.stem, MANUAL,
                            "not created automatically (it would start costing money). When you need it:",
                            f"databricks clusters create --json @\"{cf.resolve()}\""))

        for f in sorted((base / "pipelines").glob("*.json")):
            spec = json.loads(self.apply_rewrites(f.read_text().replace(old_root, self.root)))
            spec.pop("id", None)
            try:
                self.w.api_client.do("POST", "/api/2.0/pipelines", body=spec)
                R.append(Result("pipeline", spec.get("name", f.stem), OK, "created"))
            except Exception as e:
                R.append(Result("pipeline", spec.get("name", f.stem), FAILED, _err(e)))
        return R

    # ---------------------------------------------------------------- validate
    def validate(self, pkg: Path) -> list[Result]:
        base, R = pkg / "databricks", []
        ws = json.loads((base / "workspace.json").read_text())
        present = {o.path[len(self.root):].lstrip("/") for o in self._objects()}
        missing = [o["rel"] for o in ws["objects"] if o["rel"] not in present]
        R.append(Result("workspace", self.root, FAILED if missing else OK,
                        f"{len(missing)} missing, e.g. {missing[:3]}" if missing else f"{len(ws['objects'])} notebooks/files present"))
        old_jobs = {json.loads(f.read_text()).get("name") for f in (base / "jobs").glob("*.json")}
        new_jobs = {j.settings.name for j in self.w.jobs.list() if j.settings}
        if old_jobs:
            miss = sorted(old_jobs - new_jobs)
            R.append(Result("jobs", f"{len(old_jobs)} job(s)", FAILED if miss else OK, f"missing: {miss}" if miss else "all present"))
        return R
