"""
Snowflake -> Snowflake migration.

Backs up: warehouses, custom roles + grants, storage integrations (as templates),
every database's full DDL via GET_DDL (schemas, tables, views, file formats,
sequences, procedures, functions, streams, tasks, pipes), stages, internal stage
files, and all table data (unloaded as Parquet).

Restores in dependency order, retries statements that failed because something
they depend on did not exist yet, then loads the data and checks row counts.
"""
from __future__ import annotations

import csv
import json
import re
import uuid
import warnings
from io import StringIO
from pathlib import Path

import snowflake.connector
from snowflake.connector import DictCursor
from snowflake.connector.util_text import split_statements

from core.base import FAILED, MANUAL, OK, SKIPPED, Connector, Field, Resource, Result, safe_name
from connectors.snowflake_extras import SnowflakeExtrasMixin

warnings.filterwarnings("ignore", category=FutureWarning, module="snowflake")
TOOL_TAG = "cloud-account-migration"
SYSTEM_DBS = {"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"}
SYSTEM_ROLES = {"ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "PUBLIC", "ORGADMIN"}
DETAIL_KINDS = ("views", "stages", "file formats", "sequences", "streams", "tasks", "pipes")


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fq(*parts: str) -> str:
    return ".".join(ident(p) for p in parts)


def lit(value) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def file_url(path: Path) -> str:
    # works on Windows (file://C:/...) and Linux/Mac (file:///home/...)
    return "file://" + str(path.resolve()).replace("\\", "/")


_TABLE_RE = re.compile(r'(?is)^create\s+(?:or\s+replace\s+)?(?:(?:local|global)\s+)?(?:transient\s+|temporary\s+)?table\s+([^\s(]+)')


def continue_identity(stmt: str, id_map: dict, done: list) -> str:
    """In a CREATE TABLE statement, set each identity column's START to the value after the old highest ID."""
    m = _TABLE_RE.match(stmt)
    if not m:
        return stmt
    items = id_map.get(m.group(1).replace('"', "").upper())
    if not items:
        return stmt
    for it in items:
        col = re.escape(it["column"])
        pats = [rf'((?:^|[\s(,])"?{col}"?\s(?:\([^)]*\)|[^,(])*?\b(?:autoincrement|identity)\s+start\s+)(-?\d+)',
                rf'((?:^|[\s(,])"?{col}"?\s(?:\([^)]*\)|[^,(])*?\b(?:autoincrement|identity)\s*\(\s*)(-?\d+)']
        for pat in pats:
            new, n = re.subn(pat, lambda mm: mm.group(1) + str(it["next"]), stmt, count=1, flags=re.I)
            if n:
                stmt = new
                done.append(f"{it['table'].split('.')[-1]}.{it['column']} from {it['next']}")
                break
    return stmt


def first_line(err: Exception) -> str:
    """Short error text. Snowflake puts the real reason on the line AFTER 'SQL compilation error:',
    so the first few lines are joined instead of keeping only the first."""
    lines = [l.strip() for l in str(err).strip().splitlines() if l.strip()]
    return " ".join(lines[:3])[:300] if lines else type(err).__name__


class SnowflakeConnector(SnowflakeExtrasMixin, Connector):
    name = "snowflake"
    env_prefix = "SF"
    title = "Snowflake"
    fields = [
        Field("ACCOUNT", "Account identifier", required=True,
              help="ORGNAME-ACCOUNTNAME. In Snowsight run: SELECT CURRENT_ORGANIZATION_NAME()||'-'||CURRENT_ACCOUNT_NAME();"),
        Field("USER", "Username", required=True),
        Field("PASSWORD", "Password", secret=True),
        Field("AUTHENTICATOR", "Login type", options=("", "username_password_mfa", "externalbrowser"),
              help="Empty = normal password. username_password_mfa = Duo approval. externalbrowser = Google/Microsoft SSO."),
        Field("ROLE", "Role", default="ACCOUNTADMIN"),
        Field("WAREHOUSE", "Warehouse", default="COMPUTE_WH"),
        Field("AZURE_TENANT_ID", "Azure tenant ID for storage integrations", sides=("TGT",), advanced=True,
              help="Only if your Azure storage moved to a new Azure account."),
        Field("URL_REWRITE", "Replace storage URLs", sides=("TGT",), advanced=True,
              help="oldacct.blob.core.windows.net=>newacct.blob.core.windows.net"),
    ]

    # ------------------------------------------------------------------ connect
    def connect(self):
        params = {
            "account": self.env("ACCOUNT", required=True),
            "user": self.env("USER", required=True),
            "role": self.env("ROLE", "ACCOUNTADMIN"),
            "warehouse": self.env("WAREHOUSE", "COMPUTE_WH"),
            "client_session_keep_alive": True,
            "session_parameters": {"QUERY_TAG": TOOL_TAG},   # so the tool's own queries are left out of history export
        }
        auth = self.env("AUTHENTICATOR")          # externalbrowser (SSO) or username_password_mfa (Duo MFA)
        if auth:
            params["authenticator"] = auth
            if self.env("PASSWORD"):
                params["password"] = self.env("PASSWORD")
            if self.env("PASSCODE"):              # 6-digit code from the Duo app, if push doesn't work
                params["passcode"] = self.env("PASSCODE")
        else:
            params["password"] = self.env("PASSWORD", required=True)
        self.conn = snowflake.connector.connect(**params)
        self.only = {d.upper() for d in self.env_list("DATABASES")}
        self.skip_data = (self.env("SKIP_DATA", "false") or "").lower() == "true"
        self.inv: dict | None = None
        # unique scratch folder in your user stage (@~) - only files this run creates are ever removed
        self.work = f"@~/_migration_{uuid.uuid4().hex[:12]}"

    def close(self):
        if getattr(self, "conn", None):
            self.conn.close()

    def explain(self, err: Exception) -> str:
        low = str(err).lower()
        which = "old" if self.side == "SRC" else "new"
        if "incorrect username or password" in low:
            return (f"Wrong username or password for the {which} account. If unsure, an ACCOUNTADMIN can reset it in "
                    "Snowsight: ALTER USER <name> SET PASSWORD = '...';")
        if ("250001" in low and "404" in low) or "could not connect to snowflake backend" in low or "account" in low and "not found" in low:
            return (f"The {which} account identifier isn't right. In Snowsight run "
                    "SELECT CURRENT_ORGANIZATION_NAME()||'-'||CURRENT_ACCOUNT_NAME(); and paste the result "
                    "(looks like ABCDEFG-XY12345, not the short locator).")
        if "mfa" in low or "multi-factor" in low or "duo" in low:
            return f"The {which} account requires MFA: set Login type to 'username_password_mfa' and approve on your phone."
        if "is not allowed to access snowflake" in low or "network policy" in low:
            return f"The {which} account's network policy blocks this computer's IP address. Ask an admin to allow it."
        if "warehouse" in low and ("does not exist" in low or "not authorized" in low):
            return f"That warehouse doesn't exist in the {which} account (or the role can't use it). Run SHOW WAREHOUSES; and use one of them."
        if "role" in low and ("does not exist" in low or "not authorized" in low or "is not assigned" in low):
            return f"The role isn't available to this user in the {which} account. Use ACCOUNTADMIN, or a role granted to you."
        return super().explain(err)

    def q(self, sql: str) -> list[dict]:
        with self.conn.cursor(DictCursor) as cur:
            cur.execute(sql)
            try:
                return cur.fetchall()
            except Exception:
                return []

    def scalar(self, sql: str):
        with self.conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None

    # ----------------------------------------------------------------- discover
    def _databases(self) -> list[str]:
        out = []
        for r in self.q("SHOW DATABASES"):
            n = r["name"]
            if n.upper() in SYSTEM_DBS or r.get("origin") or (r.get("kind") or "STANDARD").upper() != "STANDARD":
                continue                      # skip system, shared and app databases
            if self.only and n.upper() not in self.only:
                continue
            out.append(n)
        return out

    def _show(self, what: str, db: str) -> list[dict]:
        try:
            rows = self.q(f"SHOW {what} IN DATABASE {ident(db)}")
        except Exception:
            return []
        return [r for r in rows if r.get("schema_name", "") != "INFORMATION_SCHEMA"]

    def _db_detail(self, db: str) -> dict:
        d = {"schemas": [r["name"] for r in self.q(f"SHOW SCHEMAS IN DATABASE {ident(db)}")
                         if r["name"] != "INFORMATION_SCHEMA"]}
        d["tables"] = [r for r in self._show("TABLES", db)
                       if r.get("is_external", "N") != "Y" and r.get("is_dynamic", "N") != "Y"
                       and (r.get("kind") or "TABLE") != "TEMPORARY"]
        for what in DETAIL_KINDS:
            d[what] = self._show(what.upper(), db)
        d["procedures"] = [r for r in self._show("PROCEDURES", db) if r.get("is_builtin", "N") != "Y"]
        d["functions"] = self._show("USER FUNCTIONS", db)
        return d

    def _custom_roles(self) -> list[str]:
        return [r["name"] for r in self.q("SHOW ROLES")
                if r["name"].upper() not in SYSTEM_ROLES and not r["name"].upper().startswith("SNOWFLAKE")]

    def _warehouses(self):
        return [w for w in self.q("SHOW WAREHOUSES") if "$" not in w["name"]]

    def _integrations(self):
        return [i for i in self.q("SHOW INTEGRATIONS") if "$" not in i["name"]]

    def _workspaces(self) -> list[dict]:
        for sql in ("SHOW WORKSPACES IN DATABASE USER$", "SHOW WORKSPACES"):
            try:
                rows = self.q(sql)
                return [{"name": r["name"], "db": r.get("database_name") or "USER$",
                         "schema": r.get("schema_name") or "PUBLIC"} for r in rows]
            except Exception:
                continue
        return []

    @staticmethod
    def _ws_url(w: dict, version: str) -> str:
        db = "USER$" if w["db"].upper().startswith("USER$") else ident(w["db"])
        return f"snow://workspace/{db}.{ident(w['schema'])}.{ident(w['name'])}/versions/{version}/"

    def discover(self) -> list[Resource]:
        self.inv = {db: self._db_detail(db) for db in self._databases()}
        res = []
        for db, d in self.inv.items():
            res.append(Resource("database", db, {k: len(v) for k, v in d.items()}))
            for t in d["tables"]:
                res.append(Resource("table", f"{db}.{t['schema_name']}.{t['name']}", {"rows": t.get("rows")}))
        for w in self._warehouses():
            res.append(Resource("warehouse", w["name"], {"size": w.get("size")}))
        for r in self._custom_roles():
            res.append(Resource("role", r))
        for i in self._integrations():
            res.append(Resource("integration", i["name"], {"type": i.get("type")}))
        for w in self._workspaces():
            res.append(Resource("workspace", w["name"]))
        return res

    # ------------------------------------------------------------------- backup
    def backup(self, pkg: Path) -> list[Result]:
        if self.inv is None:
            self.discover()
        base = pkg / "snowflake"
        (base / "databases").mkdir(parents=True, exist_ok=True)
        (base / "inventory_detail.json").write_text(json.dumps(self.inv, indent=2, default=str))
        R: list[Result] = []

        self.log("Saving warehouses, roles and integrations ...")
        # 1. warehouses
        wh = []
        for w in self._warehouses():
            size = (w.get("size") or "X-Small").upper()
            try:
                suspend = int(w.get("auto_suspend") or 60)
            except (TypeError, ValueError):
                suspend = 60
            wh.append(f"CREATE WAREHOUSE IF NOT EXISTS {ident(w['name'])} WAREHOUSE_SIZE={lit(size)} "
                      f"AUTO_SUSPEND={suspend} AUTO_RESUME=TRUE INITIALLY_SUSPENDED=TRUE;")
            R.append(Result("warehouse", w["name"], OK, size))
        (base / "warehouses.sql").write_text("\n".join(wh))

        # 2. roles and grants
        roles, grants = self._custom_roles(), []
        for role in roles:
            n = 0
            try:
                for g in self.q(f"SHOW GRANTS TO ROLE {ident(role)}"):
                    priv, on, obj = g["privilege"], g["granted_on"], g["name"]
                    if priv == "OWNERSHIP" or obj.upper().startswith("SNOWFLAKE"):
                        continue
                    if on == "ROLE":
                        grants.append(f"GRANT ROLE {obj} TO ROLE {ident(role)};")
                    elif on == "ACCOUNT":
                        grants.append(f"GRANT {priv} ON ACCOUNT TO ROLE {ident(role)};")
                    else:
                        grants.append(f"GRANT {priv} ON {on.replace('_', ' ')} {obj} TO ROLE {ident(role)};")
                    n += 1
                R.append(Result("role", role, OK, f"{n} grants captured"))
            except Exception as e:
                R.append(Result("role", role, FAILED, first_line(e)))
        (base / "roles.sql").write_text("\n".join(f"CREATE ROLE IF NOT EXISTS {ident(r)};" for r in roles))
        (base / "grants.sql").write_text("\n".join(grants))

        # 3. storage integrations (saved as templates - they need the NEW cloud tenant/role)
        integrations, other_integrations = [], []
        for i in self._integrations():
            n, typ = i["name"], (i.get("type") or "").upper()
            if typ != "EXTERNAL_STAGE":
                try:
                    ddl = self.scalar(f"SELECT GET_DDL('INTEGRATION', {lit(n)})")
                except Exception:
                    ddl = ""
                other_integrations.append({"name": n, "type": typ, "ddl": ddl})
                R.append(Result("integration", n, OK, f"{typ} integration saved - restore shows the statement to finish it"))
                continue
            props = {p["property"]: p["property_value"] for p in self.q(f"DESC INTEGRATION {ident(n)}")}
            provider = (props.get("STORAGE_PROVIDER") or "").upper()
            locs = ", ".join(lit(x.strip()) for x in (props.get("STORAGE_ALLOWED_LOCATIONS") or "").split(",") if x.strip())
            sql = (f"CREATE STORAGE INTEGRATION IF NOT EXISTS {ident(n)} TYPE=EXTERNAL_STAGE "
                   f"STORAGE_PROVIDER={lit(provider)} ENABLED=TRUE ")
            if provider == "AZURE":
                sql += "AZURE_TENANT_ID='{{AZURE_TENANT_ID}}' "
            elif provider == "S3":
                sql += "STORAGE_AWS_ROLE_ARN='{{AWS_ROLE_ARN}}' "
            sql += f"STORAGE_ALLOWED_LOCATIONS=({locs});"
            integrations.append({"name": n, "provider": provider, "sql": sql, "original": props})
            R.append(Result("integration", n, OK, f"{provider} storage integration captured as template"))
        (base / "integrations.json").write_text(json.dumps(integrations, indent=2))
        (base / "other_integrations.json").write_text(json.dumps(other_integrations, indent=2))

        self.log("Saving database definitions ...")
        # 4. full DDL of each database
        all_ddl: list[str] = []
        for db, d in self.inv.items():
            try:
                ddl = self.scalar(f"SELECT GET_DDL('DATABASE', {lit(ident(db))}, TRUE)")
                all_ddl.append(ddl)
                (base / "databases" / f"{safe_name(db)}.sql").write_text(ddl, encoding="utf-8")
                R.append(Result("database", db, OK, ", ".join(f"{len(v)} {k}" for k, v in d.items() if v)))
            except Exception as e:
                R.append(Result("database", db, FAILED, first_line(e)))
            if d["tasks"]:
                R.append(Result("tasks", db, OK,
                                f"{len(d['tasks'])} task(s) saved - created switched off; restore gives the command to start them"))

        # 4b. everything else in the account: census, users, monitors, parameters, policies, tags, apps ...
        census = self._census(list(self.inv))
        R += self._backup_extras(base, census, "\n".join(all_ddl))
        R += self._backup_apps(base, census)

        # 4b2. identity / autoincrement columns: remember the next value so new rows don't reuse old IDs
        identity = []
        for db in self.inv:
            try:
                cols = self.q(f"SELECT table_schema, table_name, column_name, identity_increment "
                              f"FROM {ident(db)}.INFORMATION_SCHEMA.COLUMNS WHERE is_identity = 'YES'")
            except Exception:
                continue
            for c in cols:
                c = {k.lower(): v for k, v in c.items()}
                tfq = fq(db, c["table_schema"], c["table_name"])
                try:
                    mx = self.scalar(f"SELECT MAX({ident(c['column_name'])}) FROM {tfq}")
                except Exception:
                    continue
                if mx is not None:
                    step = int(c.get("identity_increment") or 1)
                    identity.append({"table": f"{db}.{c['table_schema']}.{c['table_name']}",
                                     "column": c["column_name"], "next": int(mx) + step})
        (base / "identity.json").write_text(json.dumps(identity, indent=2))
        if identity:
            R.append(Result("identity columns", f"{len(identity)} column(s)", OK,
                            "next values saved - new rows in the new account won't reuse old IDs"))

        # 4c. streams: saved separately and created AFTER the data is loaded (see restore)
        streams = []
        for db, d in self.inv.items():
            for r in d.get("streams", []):
                sfq = fq(db, r["schema_name"], r["name"])
                try:
                    streams.append({"fq": sfq, "source": r.get("table_name") or "",
                                    "source_type": r.get("source_type") or "",
                                    "ddl": self.scalar(f"SELECT GET_DDL('STREAM', {lit(sfq)}, TRUE)")})
                except Exception as e:
                    R.append(Result("stream", sfq, FAILED, f"could not read its definition: {first_line(e)}"))
        (base / "streams.json").write_text(json.dumps(streams, indent=2))

        self.log("Saving stages and their files ...")
        # 5. stages (+ files inside internal stages)
        stages = []
        for db, d in self.inv.items():
            for s in d["stages"]:
                sch, sn = s["schema_name"], s["name"]
                name, typ = fq(db, sch, sn), (s.get("type") or "").upper()
                sql = f"CREATE STAGE IF NOT EXISTS {name}"
                if typ.startswith("EXTERNAL"):
                    url = str(s.get("url") or "").strip('[]" ')
                    sql += f" URL={lit(url)}"
                    if s.get("storage_integration"):
                        sql += f" STORAGE_INTEGRATION={ident(s['storage_integration'])}"
                fmt = self._stage_format(name)
                if fmt:
                    sql += f" FILE_FORMAT={fmt}"
                entry = {"fq": name, "type": typ, "sql": sql + ";"}
                if typ.startswith("EXTERNAL") and str(s.get("has_credentials", "")).upper() in ("Y", "TRUE"):
                    entry["needs_credentials"] = True
                    R.append(Result("stage", name, OK, "saved without its token/keys - restore gives the statement to add a fresh one "
                                                           "or switch it to a storage integration"))
                elif typ.startswith("INTERNAL") and not self.skip_data:
                    rel = Path("stages") / safe_name(db) / safe_name(sch) / safe_name(sn)
                    try:
                        n = self._download_stage(name, base / rel)
                        entry["files_dir"] = rel.as_posix()
                        R.append(Result("stage", name, OK, f"internal stage, {n} file(s) saved"))
                    except Exception as e:
                        R.append(Result("stage", name, FAILED, first_line(e)))
                else:
                    R.append(Result("stage", name, OK, "external stage definition saved"))
                stages.append(entry)
        (base / "stages.json").write_text(json.dumps(stages, indent=2))

        # 6. table data
        tables = []
        for db, d in self.inv.items():
            for t in d["tables"]:
                sch, tn = t["schema_name"], t["name"]
                name = fq(db, sch, tn)
                rel = Path("data") / safe_name(db) / safe_name(sch) / safe_name(tn)
                entry = {"fq": name, "dir": rel.as_posix(), "rows": None,
                         "stage": f"{self.work}/{safe_name(db)}/{safe_name(sch)}/{safe_name(tn)}/"}
                try:
                    entry["rows"], entry["hash"] = self._fingerprint(name)
                    entry["columns"] = self._columns(name)
                    entry["semi"] = self._semi_columns(name, entry["columns"])
                    if self.skip_data:
                        entry["dir"] = None
                        R.append(Result("table", name, SKIPPED, "SKIP_DATA=true - structure only"))
                    elif entry["rows"] == 0:
                        R.append(Result("table", name, OK, "empty table"))
                    else:
                        self.log(f"Exporting {db}.{sch}.{tn} ({entry['rows']:,} rows)")
                        files = self._export_table(name, entry["stage"], base / rel)
                        R.append(Result("table", name, OK, f"{entry['rows']:,} rows exported ({files} file(s))"))
                except Exception as e:
                    R.append(Result("table", name, FAILED, first_line(e)))
                tables.append(entry)
        (base / "tables.json").write_text(json.dumps(tables, indent=2))
        try:
            self.q(f"REMOVE {self.work}/")
        except Exception:
            pass

        self.log("Saving worksheets / Workspaces ...")
        R += self._backup_workspaces(base)
        self.log("Saving query history ...")
        R += self._backup_query_history(base)
        return R

    def _backup_workspaces(self, base: Path) -> list[Result]:
        R, saved = [], []
        for w in self._workspaces():
            root = self._ws_url(w, "head").rsplit("versions/", 1)[0]       # snow://workspace/<db>.<sch>.<name>/
            local_root = base / "workspaces" / safe_name(w["name"])
            files, failed, version, errors = [], [], "head", {}
            try:
                for row in self.q(f"LIST {lit(root + 'versions/head/')}"):
                    m = re.search(r"versions/([^/]+)/(.*)$", row.get("name", ""))
                    if not m:
                        continue
                    version, rel = m.group(1), m.group(2)                # Snowflake reports e.g. versions/live/...
                    if not rel or rel.endswith("/") or rel.endswith(".folder") or rel.startswith((".temp/", ".snowflake/")):
                        continue                                         # Snowsight's own UI files, not your work
                    dest = local_root.joinpath(*[safe_name(x) for x in rel.split("/")[:-1]])
                    dest.mkdir(parents=True, exist_ok=True)
                    err = self._get_workspace_file(root, version, rel, dest)
                    if err is None:
                        files.append(rel)
                    else:
                        failed.append(rel)
                        errors[rel] = err
                saved.append({**w, "files": files, "dir": local_root.relative_to(base).as_posix()})
                if errors:
                    (base / "workspace_errors.json").write_text(json.dumps(errors, indent=2))
                if failed:
                    first = next(iter(errors.values()))
                    R.append(Result("workspace", w["name"], FAILED,
                                    f"{len(files)} downloaded; {len(failed)} could not be, e.g. {failed[0]} -> "
                                    f"{first[:300]}  (all errors: snowflake/workspace_errors.json)"))
                else:
                    R.append(Result("workspace", w["name"], OK, f"{len(files)} worksheet/file(s) downloaded"))
            except Exception as e:
                R.append(Result("workspace", w["name"], FAILED, first_line(e)))
        (base / "workspaces.json").write_text(json.dumps(saved, indent=2))
        return R

    def _get_workspace_file(self, root: str, version: str, rel: str, dest: Path) -> str | None:
        """Try every way Snowflake offers to fetch one workspace file. Returns None or the errors seen."""
        name = rel.split("/")[-1]
        folder = rel[: -len(name)]
        target = lit(file_url(dest) + "/")
        tries = []
        for v in dict.fromkeys([version, "live", "head", "last"]):
            tries.append((f"GET {v}", f"GET {lit(root + f'versions/{v}/' + rel)} {target}"))
        for v in dict.fromkeys([version, "head"]):                    # whole parent folder, then pick the file
            tries.append((f"GET folder {v}", f"GET {lit(root + f'versions/{v}/' + folder)} {target} "
                                              f"PATTERN={lit('.*' + re.escape(name))}"))
        errs = []
        for label, sql in tries:
            try:
                self.q(sql)
                if (dest / name).exists():
                    return None
                errs.append(f"{label}: no file returned")
            except Exception as e:
                errs.append(f"{label}: {first_line(e)[:160]}")
        tmp = self._copy_files_via_stage(root, version, rel, dest)    # COPY FILES -> temp stage -> GET
        if tmp is None:
            return None
        errs.append(f"COPY FILES: {tmp}")
        return " | ".join(errs)

    def _copy_files_via_stage(self, root: str, version: str, rel: str, dest: Path) -> str | None:
        try:
            if not getattr(self, "_tmp_stage", None):
                db = next(iter(self.inv or {}), None) or self.q("SHOW DATABASES")[0]["name"]
                sch = (self.inv or {}).get(db, {}).get("schemas", ["PUBLIC"])[0]
                self._tmp_stage = f"{fq(db, sch)}.{ident('_MIGRATION_TMP_' + self.work.split('_')[-1])}"
                self.q(f"CREATE TEMPORARY STAGE IF NOT EXISTS {self._tmp_stage}")
            self.q(f"COPY FILES INTO @{self._tmp_stage}/ws/ FROM (SELECT {lit(root + f'versions/{version}/' + rel)}, "
                   f"{lit(rel)})")
            self.q(f"GET {lit('@' + self._tmp_stage + '/ws/' + rel)} {lit(file_url(dest) + '/')}")
            return None if (dest / rel.split("/")[-1]).exists() else "no file returned"
        except Exception as e:
            return first_line(e)[:160]

    def _backup_query_history(self, base: Path) -> list[Result]:
        sql = f"""SELECT start_time, database_name, schema_name, query_type, query_text
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                  WHERE user_name = CURRENT_USER() AND execution_status = 'SUCCESS'
                    AND COALESCE(query_tag, '') <> '{TOOL_TAG}'
                    AND query_type NOT IN ('SHOW','DESCRIBE','USE','ALTER_SESSION','GET_FILES','PUT_FILES',
                                           'LIST_FILES','REMOVE_FILES','COMMIT','ROLLBACK','UNKNOWN')
                  ORDER BY start_time"""
        try:
            rows = self.q(sql)
        except Exception as e:
            return [Result("query_history", "ACCOUNT_USAGE", FAILED, f"{first_line(e)} (needs ACCOUNTADMIN)")]
        d = base / "query_history"
        d.mkdir(parents=True, exist_ok=True)
        rows = [{k.lower(): v for k, v in r.items()} for r in rows]
        with open(d / "query_history.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["start_time", "database", "schema", "query_type", "query_text"])
            for r in rows:
                wr.writerow([r["start_time"], r["database_name"], r["schema_name"], r["query_type"], r["query_text"]])
        with open(d / "query_history.sql", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(f"-- {r['start_time']} | {r['database_name']}.{r['schema_name']} | {r['query_type']}\n")
                f.write(str(r["query_text"]).rstrip().rstrip(";") + ";\n\n")
        return [Result("query_history", "your queries (last 365 days)", OK,
                       f"{len(rows)} queries saved to query_history.sql / .csv")]

    def _fingerprint(self, name: str) -> tuple[int, str]:
        """Row count + order-independent hash of every value in the table (one scan)."""
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*), HASH_AGG(*) FROM {name}")
            n, h = cur.fetchone()
        return int(n or 0), str(h)

    def _semi_columns(self, name: str, columns: list) -> dict:
        """VARIANT/OBJECT/ARRAY columns -> how many values in them are plain text in the OLD table.
        Parquet stores these columns as JSON text, so restore must turn them back into JSON."""
        semi = [c for c, t in columns if any(k in str(t).upper() for k in ('"VARIANT"', '"OBJECT"', '"ARRAY"'))]
        if not semi:
            return {}
        sel = ", ".join(f"COUNT_IF(IS_VARCHAR({ident(c)}))" for c in semi)
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"SELECT {sel} FROM {name}")
                row = cur.fetchone() or [0] * len(semi)
        except Exception:
            return {c: 1 for c in semi}       # unknown -> don't convert text automatically, just flag it
        return {c: int(n or 0) for c, n in zip(semi, row)}   # empty tables return NULL instead of 0

    def _columns(self, name: str) -> list:
        try:
            return [[c["column_name"], c["data_type"]] for c in self.q(f"SHOW COLUMNS IN TABLE {name}")]
        except Exception:
            return []

    def _stage_format(self, stage_fq: str) -> str | None:
        try:
            props = self.q(f"DESC STAGE {stage_fq}")
        except Exception:
            return None
        ff = {p["property"]: p["property_value"] for p in props if p.get("parent_property") == "STAGE_FILE_FORMAT"}
        if ff.get("FORMAT_NAME"):
            return f"(FORMAT_NAME={lit(ff['FORMAT_NAME'])})"
        if ff.get("TYPE") and ff["TYPE"].upper() != "CSV":
            return f"(TYPE={ff['TYPE']})"
        return None

    def _download_stage(self, stage_fq: str, local: Path) -> int:
        files = self.q(f"LIST @{stage_fq}")
        if files:
            local.mkdir(parents=True, exist_ok=True)
            self.q(f"GET @{stage_fq} {lit(file_url(local) + '/')} PARALLEL=8")
        return len(files)

    def _export_table(self, name: str, stage: str, local: Path) -> int:
        self.q(f"REMOVE {stage}")
        self.q(f"COPY INTO {stage} FROM {name} FILE_FORMAT=(TYPE=PARQUET) HEADER=TRUE OVERWRITE=TRUE")
        local.mkdir(parents=True, exist_ok=True)
        self.q(f"GET {stage} {lit(file_url(local) + '/')} PARALLEL=8")
        self.q(f"REMOVE {stage}")
        return sum(1 for p in local.iterdir() if p.is_file())

    # ------------------------------------------------------------------ restore
    def restore(self, pkg: Path) -> list[Result]:
        base = pkg / "snowflake"
        if "workspaces" in getattr(self, "only_parts", set()):              # migrate.py restore <pkg> --only workspaces
            return self._restore_workspaces(base)
        R: list[Result] = []
        pending: list[list] = []          # [kind, name, statement, error]

        skip_streams = (base / "streams.json").exists()
        id_map: dict = {}
        id_file = base / "identity.json"
        for it in (json.loads(id_file.read_text()) if id_file.exists() else []):
            id_map.setdefault(it["table"].replace('"', "").upper(), []).append(it)
        id_done: list = []

        def run(sql: str, kind: str, name: str):
            for stmt, _ in split_statements(StringIO(self.apply_rewrites(sql)), remove_comments=True):
                stmt = stmt.strip()
                if id_map:
                    stmt = continue_identity(stmt, id_map, id_done)
                if skip_streams and re.match(r"(?is)^create\s+(or\s+replace\s+)?stream\b", stmt):
                    continue                    # streams are created after the data load (step 7b)
                if stmt and stmt != ";":
                    try:
                        self.q(stmt)
                    except Exception as e:
                        pending.append([kind, name, stmt, first_line(e)])

        def read(f):
            p = base / f
            return p.read_text(encoding="utf-8") if p.exists() else ""

        # 1. account-level objects
        run(read("warehouses.sql"), "warehouse", "warehouses")
        run(read("roles.sql"), "role", "roles")
        extras = json.loads(read("extras.json") or "[]")
        for x in extras:
            if x["order"] <= 3:                 # users, resource monitors, account parameters
                run(x["sql"], x["kind"], x["name"])

        # 2. storage integrations - filled in with the NEW tenant / role
        seen_apps: dict = {}
        for i in json.loads(read("integrations.json") or "[]"):
            sql = i["sql"]
            sql = sql.replace("{{AZURE_TENANT_ID}}", self.env("AZURE_TENANT_ID") or i["original"].get("AZURE_TENANT_ID", ""))
            sql = sql.replace("{{AWS_ROLE_ARN}}", self.env("AWS_ROLE_ARN") or i["original"].get("STORAGE_AWS_ROLE_ARN", ""))
            run(sql, "integration", i["name"])
            users_of = [x["fq"] for x in json.loads(read("stages.json") or "[]")
                        if f'STORAGE_INTEGRATION={ident(i["name"])}' in x["sql"]]
            test = "\n".join(f"LIST @{u};" for u in users_of) or f"DESC INTEGRATION {ident(i['name'])};"
            R.append(Result("integration", i["name"], MANUAL, self._integration_todo(i, seen_apps),
                            "-- after approving, test that Snowflake can read the files:\n" + test))

        self.log("Creating databases and objects ...")
        # 3. databases (schemas, tables, views, procedures, tasks, ...)
        db_files = sorted((base / "databases").glob("*.sql"))
        for f in db_files:
            run(f.read_text(encoding="utf-8"), "database", f.stem)

        for x in extras:
            if x["order"] > 3:                  # tags, policies, alerts not in the database DDL
                run(x["sql"], x["kind"], x["name"])

        # 4. stages
        stages = json.loads(read("stages.json") or "[]")
        for s in stages:
            if not s.get("needs_credentials"):
                run(s["sql"], "stage", s["fq"])

        # 5. retry what failed because a dependency didn't exist yet (views on views, pipes on stages ...)
        for _ in range(4):
            if not pending:
                break
            todo = pending[:]
            pending.clear()
            for kind, name, stmt, _err in todo:
                try:
                    self.q(stmt)
                except Exception as e:
                    pending.append([kind, name, stmt, first_line(e)])
            if len(pending) == len(todo):
                break

        if id_done:
            R.append(Result("identity columns", f"{len(id_done)} column(s)", OK,
                            "counters continue after the old highest ID: " + ", ".join(id_done[:6])))
        for f in db_files:
            n = sum(1 for p in pending if p[0] == "database" and p[1] == f.stem)
            R.append(Result("database", f.stem, OK if n == 0 else FAILED,
                            "all objects created" if n == 0 else f"{n} statement(s) failed - see below"))
        for kind, name, stmt, err in pending:
            R.append(Result(kind, name, FAILED, f"{err}  ::  {' '.join(stmt.split())[:160]}"))

        # 6. files of internal stages
        for s in stages:
            if s.get("files_dir"):
                d = base / s["files_dir"]
                try:
                    files = [p for p in d.rglob("*") if p.is_file()]
                    for p in files:
                        sub = p.parent.relative_to(d).as_posix()
                        target = f"@{s['fq']}" + ("" if sub == "." else f"/{sub}")
                        self.q(f"PUT {lit(file_url(p))} {target} AUTO_COMPRESS=FALSE OVERWRITE=TRUE")
                    R.append(Result("stage", s["fq"], OK, f"{len(files)} file(s) uploaded"))
                except Exception as e:
                    R.append(Result("stage", s["fq"], FAILED, first_line(e)))

        # 7. table data
        for t in json.loads(read("tables.json") or "[]"):
            if not t.get("dir"):
                continue
            d = base / t["dir"]
            if not d.exists() or not any(d.iterdir()):
                R.append(Result("table", t["fq"], OK, "empty table - nothing to load"))
                continue
            try:
                self.log(f"Loading {t['fq']}")
                self.q(f"REMOVE {t['stage']}")
                self.q(f"PUT {lit(file_url(d) + '/*')} {t['stage']} AUTO_COMPRESS=FALSE OVERWRITE=TRUE PARALLEL=8")
                self.q(f"COPY INTO {t['fq']} FROM {t['stage']} FILE_FORMAT=(TYPE=PARQUET) "
                       f"MATCH_BY_COLUMN_NAME=CASE_SENSITIVE PURGE=TRUE")
                note = ""
                for col, text_values in (t.get("semi") or {}).items():
                    if text_values == 0:          # old column held only JSON -> every text value came from Parquet
                        self.q(f"UPDATE {t['fq']} SET {ident(col)} = PARSE_JSON({ident(col)}::STRING) "
                               f"WHERE IS_VARCHAR({ident(col)})")
                        note = " (JSON columns restored)"
                    else:
                        R.append(Result("table", t["fq"], MANUAL,
                                        f"column {col} mixes JSON and plain text - check it after restore"))
                R.append(Result("table", t["fq"], OK, f"{t['rows']:,} rows loaded{note}"))
            except Exception as e:
                R.append(Result("table", t["fq"], FAILED, first_line(e)))

        # 7b. streams: created now, after every table, view and row exists, so they start empty
        #     (a fresh pipeline won't re-process the restored rows)
        R += self._restore_streams(base)

        # 8. notebooks / Streamlit apps, then worksheets back into Workspaces
        R += self._restore_apps(base)
        R += self._restore_workspaces(base)

        # 8b. everything the person must finish by hand, with exact commands for this account
        R += self._restore_followups(base)

        # 9. grants last (objects must exist first)
        grant_fail = []
        for stmt, _ in split_statements(StringIO(read("grants.sql")), remove_comments=True):
            if stmt.strip():
                try:
                    self.q(stmt)
                except Exception as e:
                    grant_fail.append(f"{' '.join(stmt.split())[:110]} ({first_line(e)[:200]})")
        me = self.scalar("SELECT CURRENT_USER()")
        for stmt in read("role_assignments.sql").splitlines():
            if stmt.strip():
                try:
                    self.q(stmt.replace("{{ME}}", ident(me)))
                except Exception as e:
                    grant_fail.append(f"{stmt[:110]} ({first_line(e)[:200]})")
        if grant_fail:
            for g in grant_fail[:30]:
                stmt_txt = g.split(" (")[0]
                R.append(Result("grant", stmt_txt[:120], MANUAL,
                                "refused: " + g[len(stmt_txt):].strip(" ()")[:200] + ". Fix the cause, then run:",
                                stmt_txt.rstrip(";") + ";"))
            if len(grant_fail) > 30:
                R.append(Result("grants", f"{len(grant_fail) - 30} more", MANUAL, "see report_restore.json for all"))
        elif read("grants.sql").strip():
            R.append(Result("grants", "all", OK, "grants re-applied"))
        return R

    def _restore_workspaces(self, base: Path) -> list[Result]:
        f = base / "workspaces.json"
        if not f.exists():
            return []
        R = []
        for w in json.loads(f.read_text()):
            if not w["files"]:
                continue
            tgt = {"name": w["name"], "db": "USER$", "schema": "PUBLIC"}
            name = f"USER$.PUBLIC.{ident(w['name'])}"
            local_root = base / w["dir"]
            try:
                try:
                    self.q(f"CREATE WORKSPACE IF NOT EXISTS {name}")
                except Exception:
                    pass                       # "My Workspace" is created by Snowflake itself
                try:
                    self.q(f"ALTER WORKSPACE {name} ADD LIVE VERSION FROM LAST")
                except Exception:
                    pass                       # a live version already exists
                live = self._ws_url(tgt, "live")
                for rel in w["files"]:
                    parts = rel.split("/")
                    local = local_root.joinpath(*[safe_name(x) for x in parts])
                    if not local.exists():                       # saved by the whole-folder download
                        local = next((p for p in local_root.rglob("*") if p.name == parts[-1]),
                                     local_root.joinpath(*parts))
                    folder = "/".join(parts[:-1])
                    self.q(f"PUT {lit(file_url(local))} {lit(live + (folder + '/' if folder else ''))} "
                           f"AUTO_COMPRESS=FALSE OVERWRITE=TRUE")
                try:
                    self.q(f"ALTER WORKSPACE {name} COMMIT")
                except Exception:
                    pass                       # private workspaces need no commit
                R.append(Result("workspace", w["name"], OK, f"{len(w['files'])} worksheet/file(s) uploaded"))
            except Exception as e:
                R.append(Result("workspace", w["name"], MANUAL,
                                f"auto-upload failed ({first_line(e)[:90]}). In Snowsight > Projects > Workspaces "
                                f"use '+ > Upload Folder' and pick {local_root}"))
        return R

    def _integration_todo(self, i: dict, seen_apps: dict) -> str:
        """Exact, account-specific approval steps for one storage integration, read from the NEW account."""
        name, provider = i["name"], i["provider"]
        try:
            props = {p["property"]: p["property_value"] for p in self.q(f"DESC INTEGRATION {ident(name)}")}
        except Exception:
            props = {}
        locs = [x.strip() for x in (props.get("STORAGE_ALLOWED_LOCATIONS") or "").split(",") if x.strip()]
        if provider == "AZURE" and props.get("AZURE_CONSENT_URL"):
            app = (props.get("AZURE_MULTI_TENANT_APP_NAME") or "").split("_")[0]
            accounts = sorted({re.sub(r"^azure://([^.]+)\..*$", r"\1", l) for l in locs if l.startswith("azure://")})
            where = ", ".join(accounts) or "your storage account"
            if app and app in seen_apps:
                return (f"Uses the same Snowflake app as {seen_apps[app]} (already approved there). "
                        f"Just make sure app '{app}' has the role 'Storage Blob Data Contributor' on: {where}.")
            seen_apps[app] = name
            return ("Approve once: open " + props["AZURE_CONSENT_URL"] + " and click Accept. "
                    f"Then in the Azure Portal open storage account(s) {where} -> Access Control (IAM) -> "
                    f"Add role assignment -> 'Storage Blob Data Contributor' -> select app '{app}' -> Assign. "
                    "Wait 1-2 minutes, then test with LIST @<your stage>.")
        if provider == "S3" and props.get("STORAGE_AWS_IAM_USER_ARN"):
            return ("In AWS IAM open role " + (props.get("STORAGE_AWS_ROLE_ARN") or "") + " -> Trust relationships -> "
                    "set Principal AWS to " + props["STORAGE_AWS_IAM_USER_ARN"] +
                    " and sts:ExternalId to " + (props.get("STORAGE_AWS_EXTERNAL_ID") or "") + ".")
        if provider == "GCS" and props.get("STORAGE_GCP_SERVICE_ACCOUNT"):
            return ("In Google Cloud, give service account " + props["STORAGE_GCP_SERVICE_ACCOUNT"] +
                    " the role 'Storage Object Admin' on bucket(s): " + (", ".join(locs) or "your buckets") + ".")
        return ("created - run DESC INTEGRATION " + name + " in the new account and follow its consent / "
                "permission values so Snowflake can read your cloud storage.")

    def _restore_streams(self, base: Path) -> list[Result]:
        f = base / "streams.json"
        if not f.exists():                     # backups made by older versions: reset what exists
            return self._reset_streams_legacy(base)
        todo = json.loads(f.read_text())
        R, errors = [], {}
        for _ in range(3):                     # a stream on a view may need another stream's object first
            left = []
            for st_ in todo:
                try:
                    self.q(self.apply_rewrites(st_["ddl"]))
                except Exception as e:
                    errors[st_["fq"]] = str(e)
                    left.append(st_)
            if len(left) == len(todo):
                break
            todo = left
        done = len(json.loads(f.read_text())) - len(todo)
        if done:
            R.append(Result("streams", f"{done} stream(s)", OK, "created after the data load, starting empty"))
        for st_ in todo:
            err = errors.get(st_["fq"], "")
            low = err.lower()
            src = f"{st_['source_type'].lower() or 'object'} {st_['source']}".strip()
            if "change tracking" in low or "change_tracking" in low:
                why = f"its source {src} needs change tracking. Run: ALTER TABLE <each table under it> SET CHANGE_TRACKING = TRUE; then re-run restore."
            elif "does not exist" in low or "not authorized" in low:
                why = f"its source {src} doesn't exist in the new account (check that it was restored without errors above)."
            else:
                why = first_line(Exception(err))
            R.append(Result("stream", st_["fq"], FAILED, why))
        return R

    def _reset_streams_legacy(self, base: Path) -> list[Result]:
        R, n = [], 0
        detail_file = base / "inventory_detail.json"
        if not detail_file.exists():
            return R
        for db, d in json.loads(detail_file.read_text()).items():
            for st_row in d.get("streams", []):
                sfq = fq(db, st_row["schema_name"], st_row["name"])
                try:
                    self.q(self.scalar(f"SELECT GET_DDL('STREAM', {lit(sfq)})"))
                    n += 1
                except Exception as e:
                    R.append(Result("stream", sfq, MANUAL, f"could not reset: {first_line(e)[:120]}"))
        if n:
            R.append(Result("streams", f"{n} stream(s)", OK, "reset after loading data"))
        return R

    # ----------------------------------------------------------------- validate
    def validate(self, pkg: Path) -> list[Result]:
        base = pkg / "snowflake"
        R: list[Result] = []
        for t in json.loads((base / "tables.json").read_text()):
            try:
                n, h = self._fingerprint(t["fq"])
            except Exception as e:
                R.append(Result("table", t["fq"], FAILED, f"missing in new account: {first_line(e)}"))
                continue
            if t.get("columns") and self._columns(t["fq"]) != t["columns"]:
                R.append(Result("table", t["fq"], FAILED, "column names/types differ from the old table"))
            elif t.get("dir") is None and t.get("rows"):
                R.append(Result("table", t["fq"], SKIPPED, f"structure only (old had {t['rows']:,} rows)"))
            elif n != t.get("rows"):
                R.append(Result("table", t["fq"], FAILED, f"old {t.get('rows')} rows vs new {n} rows"))
            elif t.get("hash") and h != t["hash"]:
                R.append(Result("table", t["fq"], FAILED, f"{n:,} rows but the DATA differs (content hash mismatch)"))
            else:
                R.append(Result("table", t["fq"], OK, f"{n:,} rows, columns and content hash match"))

        R += self._validate_census(base)
        src = json.loads((base / "inventory_detail.json").read_text())
        target_dbs = {r["name"] for r in self.q("SHOW DATABASES")}
        for db, d in src.items():
            if db not in target_dbs:
                R.append(Result("database", db, FAILED, "database missing in new account"))
                continue
            tgt = self._db_detail(db)
            for kind in ("schemas", "views", "procedures", "functions") + DETAIL_KINDS:
                a, b = len(d.get(kind, [])), len(tgt.get(kind, []))
                if a:
                    R.append(Result(kind, db, OK if b >= a else FAILED, f"old {a} / new {b}"))
        return R
