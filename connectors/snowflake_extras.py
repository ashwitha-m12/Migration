"""
Everything in a Snowflake account beyond databases/tables/data:
  * a full CENSUS of every object type (used by validate to prove nothing is missing)
  * users + role assignments, resource monitors, account parameters
  * masking / row-access policies, tags, alerts (when not already in the database DDL)
  * notebooks and Streamlit apps, including all their files
  * secrets / network policies / shares saved as templates (never applied blindly)
Mixed into SnowflakeConnector.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.base import FAILED, MANUAL, OK, SKIPPED, Result, safe_name

ACCOUNT_LEVEL = ["USERS", "ROLES", "WAREHOUSES", "RESOURCE MONITORS", "NETWORK POLICIES", "INTEGRATIONS",
                 "DATABASES", "SHARES"]
IN_ACCOUNT = ["SCHEMAS", "TABLES", "VIEWS", "MATERIALIZED VIEWS", "DYNAMIC TABLES", "EXTERNAL TABLES",
              "EVENT TABLES", "ICEBERG TABLES", "STAGES", "FILE FORMATS", "SEQUENCES", "STREAMS", "TASKS",
              "PIPES", "PROCEDURES", "USER FUNCTIONS", "SECRETS", "TAGS", "MASKING POLICIES",
              "ROW ACCESS POLICIES", "ALERTS", "NOTEBOOKS", "STREAMLITS", "GIT REPOSITORIES"]
# types the tool deliberately does not auto-create (credentials / safety) -> MANUAL, not FAILED, if missing
NOT_AUTO = {"SHARES", "SECRETS", "NETWORK POLICIES", "GIT REPOSITORIES"}
SKIP_PARAMS = ("NETWORK_POLICY", "SAML", "SSO", "PASSWORD", "ENCRYPTION", "REKEYING", "MFA", "OAUTH",
               "PERIODIC_DATA", "ALLOW_ID_TOKEN", "CLIENT_ENCRYPTION")
SYSTEM_USERS = {"SNOWFLAKE"}


class SnowflakeExtrasMixin:
    # ------------------------------------------------------------------ census
    def _census(self, scope_dbs) -> dict:
        """{type: sorted list of names} for every object type this account has."""
        from connectors.snowflake_connector import SYSTEM_DBS, SYSTEM_ROLES
        scope = {d.upper() for d in scope_dbs}
        out = {}
        for t in ACCOUNT_LEVEL + IN_ACCOUNT:
            sql = f"SHOW {t}" if t in ACCOUNT_LEVEL else f"SHOW {t} IN ACCOUNT"
            try:
                rows = self.q(sql)
            except Exception:
                continue                                    # type not available in this edition/region
            names = set()
            for r in rows:
                n = r.get("name", "")
                if "$" in n or r.get("is_builtin") == "Y":
                    continue
                if t in ACCOUNT_LEVEL:
                    if t == "DATABASES" and (n.upper() in SYSTEM_DBS or n.upper().startswith("USER$")
                                             or (scope and n.upper() not in scope) or r.get("origin")):
                        continue
                    if t == "ROLES" and n.upper() in SYSTEM_ROLES:
                        continue
                    if t == "USERS" and n.upper() in SYSTEM_USERS:
                        continue
                    if t == "SHARES" and ((r.get("database_name") or "").upper() in SYSTEM_DBS
                                          or n.upper() in ("ACCOUNT_USAGE", "SAMPLE_DATA")
                                          or "SNOWFLAKE" in (r.get("owner_account") or "").upper()):
                        continue                            # Snowflake's own shares exist in every account
                    names.add(n)
                else:
                    db = r.get("database_name") or r.get("catalog_name") or ""
                    if (db.upper() in SYSTEM_DBS or db.upper().startswith("USER$") or "$" in db
                            or r.get("schema_name") == "INFORMATION_SCHEMA" or n == "INFORMATION_SCHEMA"
                            or (scope and db.upper() not in scope)):
                        continue
                    names.add(f"{db}.{r['schema_name']}.{n}" if r.get("schema_name") else f"{db}.{n}")
            if names:
                out[t] = sorted(names)
        return out

    # ------------------------------------------------------------------ backup
    def _backup_extras(self, base: Path, census: dict, db_ddl: str) -> list[Result]:
        from connectors.snowflake_connector import first_line, fq, ident, lit
        R, extras = [], []                       # extras: {"kind","name","sql","order"}
        me = self.scalar("SELECT CURRENT_USER()")

        # users (passwords can never be read -> created without one)
        for u in self.q("SHOW USERS"):
            n = u["name"]
            if n.upper() in SYSTEM_USERS or "$" in n or n == me:
                continue
            props = []
            for key, col in (("LOGIN_NAME", "login_name"), ("DISPLAY_NAME", "display_name"), ("EMAIL", "email"),
                             ("DEFAULT_ROLE", "default_role"), ("DEFAULT_WAREHOUSE", "default_warehouse"),
                             ("COMMENT", "comment")):
                if u.get(col):
                    props.append(f"{key}={lit(u[col])}")
            extras.append({"kind": "user", "name": n, "order": 1,
                           "sql": f"CREATE USER IF NOT EXISTS {ident(n)} {' '.join(props)} MUST_CHANGE_PASSWORD=TRUE;"})
            R.append(Result("user", n, OK, "saved (passwords can't be read) - the restore report gives the command to set one"))

        # who has which role (your own user is mapped to whoever runs the restore)
        assigns = []
        from connectors.snowflake_connector import SYSTEM_ROLES
        for role in census.get("ROLES", []) + ["SYSADMIN", "SECURITYADMIN", "USERADMIN"]:
            try:
                for g in self.q(f"SHOW GRANTS OF ROLE {ident(role)}"):
                    who, kind = g.get("grantee_name"), (g.get("granted_to") or "").upper()
                    if kind == "USER" and who and who.upper() not in SYSTEM_USERS:
                        target = "{{ME}}" if who == me else ident(who)
                        assigns.append(f"GRANT ROLE {ident(role)} TO USER {target};")
                    elif kind == "ROLE" and who and not (role.upper() in SYSTEM_ROLES and who.upper() in SYSTEM_ROLES):
                        assigns.append(f"GRANT ROLE {ident(role)} TO ROLE {ident(who)};")
            except Exception:
                pass
        (base / "role_assignments.sql").write_text("\n".join(dict.fromkeys(assigns)))
        if assigns:
            R.append(Result("role assignments", f"{len(assigns)} grant(s)", OK, "your roles will be granted to your new user"))

        # resource monitors (+ which warehouse uses which)
        for m in self.q("SHOW RESOURCE MONITORS"):
            sql = f"CREATE RESOURCE MONITOR IF NOT EXISTS {ident(m['name'])} WITH"
            if m.get("credit_quota"):
                sql += f" CREDIT_QUOTA={int(float(m['credit_quota']))}"
            if m.get("frequency"):
                sql += f" FREQUENCY={m['frequency']} START_TIMESTAMP=IMMEDIATELY"
            trig = []
            for col, action in (("notify_at", "NOTIFY"), ("suspend_at", "SUSPEND"),
                                ("suspend_immediately_at", "SUSPEND_IMMEDIATE")):
                for pct in re.findall(r"\d+", str(m.get(col) or "")):
                    trig.append(f"ON {pct} PERCENT DO {action}")
            if trig:
                sql += " TRIGGERS " + " ".join(trig)
            extras.append({"kind": "resource monitor", "name": m["name"], "order": 2, "sql": sql + ";"})
            R.append(Result("resource monitor", m["name"], OK, "captured"))
        for w in self._warehouses():
            if w.get("resource_monitor") and w["resource_monitor"].lower() != "null":
                extras.append({"kind": "warehouse", "name": w["name"], "order": 3,
                               "sql": f"ALTER WAREHOUSE {ident(w['name'])} SET RESOURCE_MONITOR={ident(w['resource_monitor'])};"})

        # account parameters you changed (risky security ones are saved but not applied)
        params, skipped = [], []
        try:
            for p in self.q("SHOW PARAMETERS IN ACCOUNT"):
                if (p.get("level") or "").upper() != "ACCOUNT":
                    continue
                key, val, typ = p["key"], p.get("value"), (p.get("type") or "").upper()
                if any(s in key.upper() for s in SKIP_PARAMS):
                    skipped.append({"key": key, "value": val, "type": typ})
                    continue
                v = lit(val) if typ == "STRING" else val
                params.append({"kind": "account parameter", "name": key, "order": 3,
                               "sql": f"ALTER ACCOUNT SET {key} = {v};"})
        except Exception as e:
            R.append(Result("account parameters", "SHOW PARAMETERS", FAILED, first_line(e)))
        extras += params
        if params:
            R.append(Result("account parameters", f"{len(params)} setting(s)", OK, ", ".join(x["name"] for x in params)[:200]))
        (base / "security_params.json").write_text(json.dumps(skipped, indent=2))
        if skipped:
            R.append(Result("account parameters", f"{len(skipped)} security setting(s)", OK,
                            "saved, not applied automatically (could lock you out) - restore shows the commands"))

        # policies / tags / alerts not already inside the database DDL
        low = db_ddl.lower()
        for t, ddl_type, order in (("TAGS", "TAG", 5), ("MASKING POLICIES", "POLICY", 6),
                                   ("ROW ACCESS POLICIES", "POLICY", 6), ("ALERTS", "ALERT", 8)):
            for full in census.get(t, []):
                short = full.split(".")[-1].lower()
                kw = {"TAGS": "tag", "MASKING POLICIES": "masking policy",
                      "ROW ACCESS POLICIES": "row access policy", "ALERTS": "alert"}[t]
                if re.search(rf"{kw}\s+[\w.\"$]*{re.escape(short)}\b", low):
                    continue                                       # already recreated by the database DDL
                parts = full.split(".", 2)
                try:
                    ddl = self.scalar(f"SELECT GET_DDL({lit(ddl_type)}, {lit(fq(*parts))}, TRUE)")
                    extras.append({"kind": t.lower()[:-1], "name": full, "order": order, "sql": ddl})
                    R.append(Result(t.lower()[:-1], full, OK, "captured"))
                except Exception as e:
                    R.append(Result(t.lower()[:-1], full, FAILED, first_line(e)))

        # things that hold credentials or could lock you out: saved, never applied blindly.
        # The restore report turns each into an exact, ready-to-run command for this account.
        secrets, shares, repos = [], [], []
        if census.get("SECRETS"):
            for r in self.q("SHOW SECRETS IN ACCOUNT"):
                full = f"{r.get('database_name')}.{r.get('schema_name')}.{r['name']}"
                if full in census["SECRETS"]:
                    secrets.append({"fq": full, "type": (r.get("secret_type") or "").upper()})
            R.append(Result("secrets", f"{len(secrets)} secret(s)", OK, "names and types saved (values can't be read)"))
        for n in census.get("NETWORK POLICIES", []):
            try:
                desc = self.q(f"DESC NETWORK POLICY {ident(n)}")
                (base / "network_policies").mkdir(exist_ok=True)
                (base / "network_policies" / f"{safe_name(n)}.json").write_text(json.dumps({"name": n, "desc": desc}, indent=2, default=str))
                R.append(Result("network policy", n, OK, "saved, not applied automatically (wrong IPs would lock you out)"))
            except Exception as e:
                R.append(Result("network policy", n, FAILED, first_line(e)))
        if census.get("SHARES"):
            for r in self.q("SHOW SHARES"):
                if r["name"] in census["SHARES"]:
                    shares.append({"name": r["name"], "kind": (r.get("kind") or "").upper(),
                                   "owner": r.get("owner_account") or "", "database": r.get("database_name") or "",
                                   "to": r.get("to") or ""})
            R.append(Result("shares", f"{len(shares)} share(s)", OK, "saved - restore lists who must re-share what"))
        if census.get("GIT REPOSITORIES"):
            for r in self.q("SHOW GIT REPOSITORIES IN ACCOUNT"):
                full = f"{r.get('database_name')}.{r.get('schema_name')}.{r['name']}"
                if full in census["GIT REPOSITORIES"]:
                    repos.append({"fq": full, "origin": r.get("origin") or "", "api_integration": r.get("api_integration") or "",
                                  "git_credentials": r.get("git_credentials") or ""})
            R.append(Result("git repositories", f"{len(repos)} repo(s)", OK, "saved - restore gives the CREATE statements"))
        (base / "followups.json").write_text(json.dumps({"secrets": secrets, "shares": shares, "git_repos": repos}, indent=2))

        # data that is tied to the old account and cannot be moved
        (base / "extras.json").write_text(json.dumps(sorted(extras, key=lambda x: x["order"]), indent=2))
        (base / "census.json").write_text(json.dumps({**census, "_OLD_USER": [me]}, indent=2))
        return R

    def _download_versioned(self, kind: str, full: str, local: Path) -> tuple[list[str], str | None]:
        """Download all files of a notebook / Streamlit app (snow://<kind>/<fq>/versions/<v>/)."""
        from connectors.snowflake_connector import file_url, fq, lit
        for version in ("live", "last", "default", "head"):
            url = f"snow://{kind}/{fq(*full.split('.', 2))}/versions/{version}/"
            try:
                rows = self.q(f"LIST {lit(url)}")
            except Exception:
                continue
            files = []
            for row in rows:
                path = row.get("name", "")
                rel = path.split(f"versions/{version}/", 1)[1] if f"versions/{version}/" in path else path
                rel = rel.lstrip("/")
                if not rel or rel.endswith("/"):
                    continue
                dest = local.joinpath(*[safe_name(x) for x in rel.split("/")[:-1]])
                dest.mkdir(parents=True, exist_ok=True)
                self.q(f"GET {lit(url + rel)} {lit(file_url(dest) + '/')}")
                files.append(rel)
            return files, version
        return [], None

    def _backup_apps(self, base: Path, census: dict) -> list[Result]:
        from connectors.snowflake_connector import first_line, fq
        R, apps = [], []
        for t, kind in (("NOTEBOOKS", "notebook"), ("STREAMLITS", "streamlit")):
            for full in census.get(t, []):
                local_rel = Path("apps") / kind / safe_name(full)
                try:
                    props = {}
                    for p in self.q(f"DESC {kind.upper()} {fq(*full.split('.', 2))}"):
                        props.update({k.lower(): v for k, v in p.items()})
                    files, version = self._download_versioned(kind, full, base / local_rel)
                    apps.append({"kind": kind, "fq": full, "dir": local_rel.as_posix(), "files": files,
                                 "main_file": props.get("main_file"), "warehouse": props.get("query_warehouse")})
                    R.append(Result(kind, full, OK if files else MANUAL,
                                    f"{len(files)} file(s) saved" if files else
                                    "no files found via SQL - download it from Snowsight"))
                except Exception as e:
                    R.append(Result(kind, full, FAILED, first_line(e)))
        (base / "apps.json").write_text(json.dumps(apps, indent=2))
        R.append(Result("dashboards", "Snowsight dashboards", SKIPPED,
                        "dashboards have no export API - their charts' SQL is in your query history backup"))
        return R

    # ----------------------------------------------------------------- restore
    def _restore_apps(self, base: Path) -> list[Result]:
        from connectors.snowflake_connector import first_line, fq, ident, lit, file_url
        f = base / "apps.json"
        if not f.exists():
            return []
        R = []
        for a in json.loads(f.read_text()):
            if not a["files"]:
                continue
            db, sch, name = a["fq"].split(".", 2)
            stage = f"{fq(db, sch)}.{ident('_MIGRATION_ASSETS')}"
            prefix = f"{a['kind']}/{safe_name(name)}"
            try:
                self.q(f"CREATE STAGE IF NOT EXISTS {stage}")
                for rel in a["files"]:
                    parts = rel.split("/")
                    local = (base / a["dir"]).joinpath(*[safe_name(x) for x in parts])
                    sub = "/".join(parts[:-1])
                    self.q(f"PUT {lit(file_url(local))} '@{stage}/{prefix}{'/' + sub if sub else ''}/' "
                           f"AUTO_COMPRESS=FALSE OVERWRITE=TRUE")
                main = a.get("main_file") or ("notebook_app.ipynb" if a["kind"] == "notebook" else "streamlit_app.py")
                wh = a.get("warehouse") or self.env("WAREHOUSE", "COMPUTE_WH")
                self.q(f"CREATE OR REPLACE {a['kind'].upper()} {fq(db, sch, name)} FROM '@{stage}/{prefix}/' "
                       f"MAIN_FILE={lit(main)} QUERY_WAREHOUSE={ident(wh)}")
                if a["kind"] == "notebook":
                    try:
                        self.q(f"ALTER NOTEBOOK {fq(db, sch, name)} ADD LIVE VERSION FROM LAST")
                    except Exception:
                        pass
                R.append(Result(a["kind"], a["fq"], OK, f"recreated with {len(a['files'])} file(s)"))
            except Exception as e:
                R.append(Result(a["kind"], a["fq"], MANUAL,
                                f"auto-create failed ({first_line(e)[:80]}); its files are in {base / a['dir']} - "
                                f"import them in Snowsight"))
        return R


    # ------------------------------------------------------- restore follow-ups
    def _restore_followups(self, base: Path) -> list[Result]:
        """Everything a person must finish by hand, each with the exact command for THEIR account."""
        from connectors.snowflake_connector import first_line, fq, ident, lit
        R = []
        rd = lambda f, d: json.loads((base / f).read_text()) if (base / f).exists() else d
        extras = rd("extras.json", [])

        users = [x["name"] for x in extras if x["kind"] == "user"]
        if users:
            R.append(Result("users", f"{len(users)} user(s)", MANUAL,
                            "created without passwords (no tool can read them). Set one for each user:",
                            "\n".join(f"ALTER USER {ident(u)} SET PASSWORD = '<choose a password>' MUST_CHANGE_PASSWORD = TRUE;"
                                      for u in users)))

        detail = rd("inventory_detail.json", {})
        roots = []
        for db, d in detail.items():
            if not d.get("tasks"):
                continue
            try:
                rows = self.q(f"SHOW TASKS IN DATABASE {ident(db)}")
            except Exception:
                rows = d["tasks"]
            for t in rows:
                pred = str(t.get("predecessors") or "").strip()
                if pred in ("", "[]", "None", "null"):
                    roots.append(fq(db, t["schema_name"], t["name"]))
        if roots:
            R.append(Result("tasks", f"{len(roots)} task chain(s)", MANUAL,
                            "created switched off on purpose. When you want the pipeline running (uses credits), run:",
                            "\n".join(f"SELECT SYSTEM$TASK_DEPENDENTS_ENABLE({lit(r.replace(chr(34), ''))});" for r in roots)))

        alerts = [x["name"] for x in extras if x["kind"] == "alert"]
        if alerts:
            R.append(Result("alerts", f"{len(alerts)} alert(s)", MANUAL, "created suspended. To turn them on:",
                            "\n".join(f"ALTER ALERT {fq(*a.split('.', 2))} RESUME;" for a in alerts)))

        for st_ in rd("stages.json", []):
            if st_.get("needs_credentials"):
                url = (re.search(r"URL='([^']*)'", st_["sql"]) or [None, ""])[1]
                if url.startswith("azure://"):
                    cred = "CREDENTIALS = (AZURE_SAS_TOKEN = '<new SAS token>')"
                elif url.startswith("s3"):
                    cred = "CREDENTIALS = (AWS_KEY_ID = '<key id>' AWS_SECRET_KEY = '<secret key>')"
                else:
                    cred = "STORAGE_INTEGRATION = <integration name>"
                R.append(Result("stage", st_["fq"], MANUAL,
                                "not created - it used a password/token that can't be copied. Add a fresh one and run:",
                                st_["sql"].rstrip(";") + " " + cred + ";"))

        fu = rd("followups.json", {})
        templates = {"PASSWORD": "TYPE = PASSWORD USERNAME = '<username>' PASSWORD = '<password>'",
                     "GENERIC_STRING": "TYPE = GENERIC_STRING SECRET_STRING = '<value>'",
                     "OAUTH2": "TYPE = OAUTH2 API_AUTHENTICATION = <security integration> OAUTH_REFRESH_TOKEN = '<token>' OAUTH_REFRESH_TOKEN_EXPIRY_TIME = '<yyyy-mm-dd hh:mi:ss>'",
                     "CLOUD_PROVIDER_TOKEN": "TYPE = CLOUD_PROVIDER_TOKEN API_AUTHENTICATION = <integration>"}
        if fu.get("secrets"):
            R.append(Result("secrets", f"{len(fu['secrets'])} secret(s)", MANUAL,
                            "not created - secret values can't be read. Fill in each value and run:",
                            "\n".join(f"CREATE SECRET IF NOT EXISTS {fq(*x['fq'].split('.', 2))} "
                                      f"{templates.get(x['type'], 'TYPE = ' + (x['type'] or '<type>') + ' ...')};"
                                      for x in fu["secrets"])))

        npdir = base / "network_policies"
        if npdir.exists():
            for f in sorted(npdir.glob("*.json")):
                saved = json.loads(f.read_text())
                parts = []
                for row in saved.get("desc", []):
                    key, val = str(row.get("name", "")).upper(), str(row.get("value") or "")
                    if key.endswith("_LIST") and val:
                        items = [v.strip() for v in val.strip("[]").split(",") if v.strip()]
                        parts.append(f"{key} = (" + ", ".join(lit(v.strip("'\"")) for v in items) + ")")
                R.append(Result("network policy", saved["name"], MANUAL,
                                "not applied (a wrong IP list would lock you out). Check your own IP is allowed, then run:",
                                f"CREATE NETWORK POLICY IF NOT EXISTS {ident(saved['name'])} " + " ".join(parts) + ";\n"
                                f"-- only after testing a login: ALTER ACCOUNT SET NETWORK_POLICY = {ident(saved['name'])};"))

        sec = rd("security_params.json", [])
        if sec:
            R.append(Result("security settings", f"{len(sec)} setting(s)", MANUAL,
                            "not applied automatically. Review each, then run the ones you want:",
                            "\n".join(f"ALTER ACCOUNT SET {p['key']} = {lit(p['value']) if p.get('type') == 'STRING' else p['value']};"
                                       for p in sec)))

        if fu.get("shares"):
            try:
                me = self.scalar("SELECT CURRENT_ORGANIZATION_NAME() || '.' || CURRENT_ACCOUNT_NAME()")
            except Exception:
                me = "<your new account>"
            for sh in fu["shares"]:
                if sh["kind"] == "INBOUND":
                    R.append(Result("share", sh["name"], MANUAL,
                                    f"shared with you by {sh['owner'] or 'its provider'}. Ask them to add your new account {me} "
                                    f"to share '{sh['name']}'. Then run:",
                                    f"CREATE DATABASE {ident(sh['database'] or sh['name'])} FROM SHARE {sh['owner']}.{sh['name']};"))
                else:
                    R.append(Result("share", sh["name"], MANUAL,
                                    f"a share you provide (to: {sh['to'] or 'consumers'}). Recreate it, grant its objects, then re-add consumers:",
                                    f"CREATE SHARE IF NOT EXISTS {ident(sh['name'])};\n"
                                    f"GRANT USAGE ON DATABASE {ident(sh['database'])} TO SHARE {ident(sh['name'])};\n"
                                    f"ALTER SHARE {ident(sh['name'])} ADD ACCOUNTS = <consumer org.account>;"))

        for repo in fu.get("git_repos", []):
            R.append(Result("git repository", repo["fq"], MANUAL,
                            "needs its Git API integration (and token) to exist first. Then run:",
                            f"CREATE GIT REPOSITORY IF NOT EXISTS {fq(*repo['fq'].split('.', 2))} "
                            f"API_INTEGRATION = {repo['api_integration'] or '<api integration>'} "
                            + (f"GIT_CREDENTIALS = {repo['git_credentials']} " if repo["git_credentials"] else "")
                            + f"ORIGIN = {lit(repo['origin'])};"))

        for oi in rd("other_integrations.json", []):
            R.append(Result("integration", oi["name"], MANUAL,
                            f"{oi['type']} integration holds credentials, so it wasn't created. Fill in the secret values and run:",
                            oi.get("ddl") or f"-- recreate integration {oi['name']} (type {oi['type']}) as in the old account"))
        return R

    # ---------------------------------------------------------------- validate
    def _validate_census(self, base: Path) -> list[Result]:
        f = base / "census.json"
        if not f.exists():
            return []
        old = json.loads(f.read_text())
        old_me = set(old.pop("_OLD_USER", []))
        new = self._census(list(old.get("DATABASES", [])))
        R = []
        for t, names in old.items():
            have = set(new.get(t, []))
            # your own old login is replaced by the new account's login, so it is not "missing"
            missing = [n for n in names if n not in have and not (t == "USERS" and n in old_me)]
            if not missing:
                R.append(Result("census", t.lower(), OK, f"all {len(names)} present"))
            else:
                R.append(Result("census", t.lower(), MANUAL if t in NOT_AUTO else FAILED,
                                f"{len(missing)} of {len(names)} missing: {', '.join(missing[:8])}"))
        return R
