"""Offline tests: python -m pytest tests  (no cloud account needed)."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import engine, registry                       # noqa: E402
from core.base import OK, Connector, Resource, Result   # noqa: E402


# ---------------------------------------------------------------- engine test
class FakeConnector(Connector):
    name, env_prefix = "fake", "FAKE"
    store = {"SRC": {"a.txt": "hello", "b.txt": "world"}, "TGT": {}}

    def connect(self): pass
    def discover(self): return [Resource("file", k) for k in self.store[self.side]]

    def backup(self, pkg):
        (pkg / "fake").mkdir()
        for k, v in self.store["SRC"].items():
            (pkg / "fake" / k).write_text(v)
        return [Result("file", k, OK) for k in self.store["SRC"]]

    def restore(self, pkg):
        for f in (pkg / "fake").iterdir():
            self.store["TGT"][f.name] = f.read_text()
        return [Result("file", f, OK) for f in self.store["TGT"]]

    def validate(self, pkg):
        return [Result("file", k, OK if self.store["TGT"].get(k) == v else "FAILED")
                for k, v in self.store["SRC"].items()]


sys.modules["fake_mod"] = type(sys)("fake_mod")
sys.modules["fake_mod"].FakeConnector = FakeConnector
registry.PLUGINS["fake"] = "fake_mod:FakeConnector"


def test_full_migration(tmp_path):
    pkg, counts = engine.migrate("fake", str(tmp_path), log=lambda *_: None)
    assert counts["validate"]["OK"] == 2 and counts["validate"]["FAILED"] == 0
    assert (pkg / "manifest.json").exists() and (pkg / "report_validate.md").exists()


def test_unfinished_backup_is_rejected(tmp_path):
    pkg = tmp_path / "fake_x"; pkg.mkdir()
    (pkg / engine.INCOMPLETE).write_text("x")
    try:
        engine.restore(pkg, log=lambda *_: None); assert False
    except RuntimeError as e:
        assert "unfinished" in str(e)


def test_damaged_backup_is_rejected(tmp_path):
    pkg, _, _ = engine.backup("fake", str(tmp_path), log=lambda *_: None)
    (pkg / "fake" / "a.txt").write_text("tampered")
    try:
        engine.restore(pkg, log=lambda *_: None)
        assert False, "should have failed"
    except RuntimeError as e:
        assert "damaged" in str(e)


# ------------------------------------------------- simulated Snowflake account
class FakeCursor:
    def __init__(self, conn): self.conn, self.rows = conn, []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql):
        self.conn.log.append(sql)
        self.rows = self.conn.answer(sql)
    def fetchall(self): return self.rows
    def fetchone(self):
        return list(self.rows[0].values()) if self.rows and isinstance(self.rows[0], dict) else (self.rows[0] if self.rows else None)
    def close(self): pass


class FakeSnowflake:
    def __init__(self, target=False):
        self.log, self.target = [], target

    def cursor(self, *_): return FakeCursor(self)
    def close(self): pass

    def answer(self, sql):
        s = " ".join(sql.split()).upper()
        if s.startswith("SELECT CURRENT_USER()"):
            return [("NEWME" if self.target else "AR73M",)]
        if s.startswith("SHOW SHARES"):
            return [{"name": "ACCOUNT_USAGE", "database_name": "SNOWFLAKE", "kind": "INBOUND"}]
        if s.startswith("SHOW USERS"):
            return [{"name": "AR73M"}, {"name": "ANALYST_USER", "email": "a@b.c", "default_role": "ANALYST"}]
        if s.startswith("SHOW GRANTS OF ROLE"):
            return [{"granted_to": "USER", "grantee_name": "AR73M"}]
        if s.startswith("SHOW TABLES IN ACCOUNT"):
            return [{"name": "ORDERS", "schema_name": "BRONZE", "database_name": "SUPPLY_CHAIN_DB"}]
        if s.startswith("SHOW MASKING POLICIES IN ACCOUNT"):
            return [{"name": "MASK_EMAIL", "schema_name": "SILVER", "database_name": "SUPPLY_CHAIN_DB"}]
        if s.startswith("SHOW NOTEBOOKS IN ACCOUNT"):
            return [{"name": "EDA", "schema_name": "GOLD", "database_name": "SUPPLY_CHAIN_DB"}]
        if s.startswith("DESC NOTEBOOK"):
            return [{"main_file": "notebook_app.ipynb", "query_warehouse": "COMPUTE_WH"}]
        if s.startswith("LIST 'SNOW://NOTEBOOK"):
            return [{"name": "versions/live/notebook_app.ipynb"}]
        if s.startswith("GET 'SNOW://NOTEBOOK"):
            m = re.search(r"'file://(.+?)/'", sql)
            Path(m.group(1)).mkdir(parents=True, exist_ok=True)
            (Path(m.group(1)) / "notebook_app.ipynb").write_text("{}")
            return []
        if s.startswith("SELECT GET_DDL('POLICY'"):
            return [("create or replace masking policy SUPPLY_CHAIN_DB.SILVER.MASK_EMAIL as (v string) returns string -> v;",)]
        if s.startswith("SHOW DATABASES"):
            return [{"name": "SUPPLY_CHAIN_DB", "origin": "", "kind": "STANDARD"},
                    {"name": "SNOWFLAKE", "origin": "", "kind": "STANDARD"}]
        if s.startswith("SHOW SCHEMAS"):
            return [{"name": n} for n in ("BRONZE", "SILVER", "GOLD", "INFORMATION_SCHEMA")]
        if s.startswith("SHOW TABLES"):
            return [{"name": "ORDERS", "schema_name": "BRONZE", "kind": "TABLE", "is_external": "N", "rows": 3}]
        if s.startswith("SHOW STAGES"):
            return [{"name": "AZ_STAGE", "schema_name": "BRONZE", "type": "EXTERNAL", "has_credentials": "N",
                     "url": "azure://oldacct.blob.core.windows.net/raw/", "storage_integration": "AZ_INT"}]
        if s.startswith("SHOW TASKS"):
            return [{"name": "T1", "schema_name": "GOLD"}]
        if s.startswith("SHOW WAREHOUSES"):
            return [{"name": "COMPUTE_WH", "size": "X-Small", "auto_suspend": 60},
                    {"name": "SYSTEM$STREAMLIT_NOTEBOOK_WH", "size": "X-Small", "auto_suspend": 60}]
        if s.startswith("SHOW ROLES"):
            return [{"name": "ACCOUNTADMIN"}, {"name": "ANALYST"}]
        if s.startswith("SHOW GRANTS TO ROLE"):
            return [{"privilege": "USAGE", "granted_on": "DATABASE", "name": "SUPPLY_CHAIN_DB"}]
        if s.startswith("SHOW INTEGRATIONS"):
            return [{"name": "AZ_INT", "type": "EXTERNAL_STAGE"}]
        if s.startswith("DESC INTEGRATION"):
            return [{"property": "STORAGE_PROVIDER", "property_value": "AZURE"},
                    {"property": "AZURE_TENANT_ID", "property_value": "old-tenant"},
                    {"property": "STORAGE_ALLOWED_LOCATIONS", "property_value": "azure://oldacct.blob.core.windows.net/raw/"}]
        if s.startswith("SHOW COLUMNS"):
            return [{"column_name": "ID", "data_type": '{"type":"FIXED"}'},
                    {"column_name": "RAW_ROW", "data_type": '{"type":"VARIANT","nullable":true}'}]
        if s.startswith("SELECT COUNT_IF(IS_VARCHAR"):
            return [(None,)]                                         # what Snowflake returns for an empty table
        if s.startswith("SHOW STREAMS"):
            return [{"name": "ORDERS_STRM", "schema_name": "BRONZE"}]
        if s.startswith("SELECT GET_DDL('STREAM'"):
            return [("create or replace stream SUPPLY_CHAIN_DB.BRONZE.ORDERS_STRM on table SUPPLY_CHAIN_DB.BRONZE.ORDERS;",)]
        if self.target and s.startswith("GRANT USAGE ON DATABASE"):
            raise Exception("001003 (42000): SQL compilation error:\nsyntax error line 1 at position 6 unexpected 'USAGE'.")
        if s.startswith("SHOW GRANTS OF ROLE"):
            return [{"granted_to": "USER", "grantee_name": "AR73M"}, {"granted_to": "ROLE", "grantee_name": "ACCOUNTADMIN"}]
        if s.startswith("SHOW WORKSPACES"):
            return [{"name": "My Workspace", "database_name": "USER$AR73M", "schema_name": "PUBLIC"}]
        if s.startswith("LIST 'SNOW://WORKSPACE"):
            return [{"name": "versions/live/01_setup.sql"}, {"name": "versions/live/gold/05_gold.sql"},
                    {"name": "versions/live/Snowflake_micro_practice/Fundamentals & Architecture/2.Data  Warehouse/02_DW.sql"},
                    {"name": "versions/live/.temp/viz/abc.viz"}, {"name": "versions/live/gold/.folder"}]
        if s.startswith("GET 'SNOW://WORKSPACE"):
            m = re.search(r"'file://(.+?)/'", sql)
            name = sql.split("'")[1].rsplit("/", 1)[-1]
            if "versions/head/" in sql and not sql.split("'")[1].endswith("/"):
                raise Exception("253006: the file does not exist")       # real files live under versions/live/
            if name == "02_DW.sql":                                      # 'legacy' file: only folder GET works
                raise Exception("253006: the file does not exist")
            if name == "" and "PATTERN" in s:
                Path(m.group(1)).mkdir(parents=True, exist_ok=True)
                fname = re.search(r"PATTERN='\.\*(.+?)'", sql).group(1).replace("\\", "")
                (Path(m.group(1)) / fname).write_text("select 2;")
                return []
            if name == "":                                               # whole-folder GET
                Path(m.group(1)).mkdir(parents=True, exist_ok=True)
                (Path(m.group(1)) / "02_DW.sql").write_text("select 2;")
                return []
            Path(m.group(1)).mkdir(parents=True, exist_ok=True)
            (Path(m.group(1)) / name).write_text("select 1;")
            return []
        if "ACCOUNT_USAGE.QUERY_HISTORY" in s:
            return [{"START_TIME": "2026-10-01", "DATABASE_NAME": "SUPPLY_CHAIN_DB", "SCHEMA_NAME": "BRONZE",
                     "QUERY_TYPE": "CREATE_TABLE", "QUERY_TEXT": "create table x(a int)"}]
        if s.startswith("SELECT GET_DDL"):
            return [("create or replace database SUPPLY_CHAIN_DB;\ncreate or replace schema SUPPLY_CHAIN_DB.BRONZE;\n"
                     "create or replace view SUPPLY_CHAIN_DB.GOLD.V as select * from SUPPLY_CHAIN_DB.BRONZE.ORDERS;\n"
                     "create or replace TABLE SUPPLY_CHAIN_DB.BRONZE.ORDERS (ID NUMBER);",)]
        if s.startswith("SELECT COUNT(*), HASH_AGG(*)"):
            return [(3, 987654321)]
        if s.startswith("GET "):
            m = re.search(r"'FILE://(.+?)/'", sql, re.I)
            d = Path(sql[m.start(1):m.end(1)])
            d.mkdir(parents=True, exist_ok=True)
            (d / "data_0_0_0.snappy.parquet").write_bytes(b"PAR1")
            return []
        if self.target and "CREATE OR REPLACE VIEW" in s and not any("TABLE SUPPLY_CHAIN_DB.BRONZE.ORDERS" in x.upper() for x in self.log[:-1]):
            raise Exception("Object 'ORDERS' does not exist")     # dependency not created yet -> must be retried
        return []


def _sf(side, conn, monkeypatch):
    from connectors import snowflake_connector as sc
    monkeypatch.setattr(sc.snowflake.connector, "connect", lambda **k: conn)
    for k in ("ACCOUNT", "USER", "PASSWORD"):
        monkeypatch.setenv(f"{side}_SF_{k}", "x")
    c = sc.SnowflakeConnector(side)
    c.connect()
    return c


def test_snowflake_backup_and_restore(tmp_path, monkeypatch):
    src = _sf("SRC", FakeSnowflake(), monkeypatch)
    inv = src.discover()
    assert "SNOWFLAKE" not in [r.name for r in inv if r.kind == "database"]
    pkg = tmp_path / "pkg"
    res = src.backup(pkg)
    assert not [r for r in res if r.status == "FAILED"], res
    assert (pkg / "snowflake/databases/SUPPLY_CHAIN_DB.sql").exists()
    assert list((pkg / "snowflake/data").rglob("*.parquet"))
    assert (pkg / "snowflake/workspaces/My_Workspace/gold/05_gold.sql").exists()
    assert list((pkg / "snowflake/workspaces").rglob("02_DW.sql"))
    assert not list((pkg / "snowflake/workspaces").rglob("*.viz"))
    ws = [r for r in res if r.kind == "workspace"][0]
    assert ws.status == "OK" and ws.message.startswith("3 "), ws
    assert not [r for r in res if r.kind == "share"]
    assert "create table x" in (pkg / "snowflake/query_history/query_history.sql").read_text()
    assert "SYSTEM$" not in (pkg / "snowflake/warehouses.sql").read_text()

    monkeypatch.setenv("TGT_SF_AZURE_TENANT_ID", "new-tenant")
    monkeypatch.setenv("TGT_SF_URL_REWRITE", "oldacct.blob=>newacct.blob")
    tgt_conn = FakeSnowflake(target=True)
    tgt = _sf("TGT", tgt_conn, monkeypatch)
    res = tgt.restore(pkg)
    log = "\n".join(tgt_conn.log)
    assert "new-tenant" in log and "newacct.blob" in log and "oldacct" not in log
    assert "COPY INTO" in log and "MATCH_BY_COLUMN_NAME" in log
    assert "versions/live/gold/" in log
    assert 'SET "RAW_ROW" = PARSE_JSON("RAW_ROW"::STRING)' in log          # JSON column turned back into JSON
    load_at = log.index("COPY INTO \"SUPPLY_CHAIN_DB\"")
    assert log.lower().index("create or replace stream") > load_at        # streams created only AFTER data load
    assert (pkg / "snowflake/streams.json").exists()
    g = [r for r in res if r.kind == "grant"]
    assert g and "unexpected 'USAGE'" in g[0].message, g                    # the real reason is shown
    assert 'GRANT ROLE "SYSADMIN" TO ROLE "ACCOUNTADMIN"' not in log      # built-in role links skipped
    assert not [r for r in res if r.status == "FAILED"], [r for r in res if r.status == "FAILED"]
    assert 'GRANT ROLE "ANALYST" TO USER "NEWME"' in log            # old login mapped to new login
    assert "CREATE OR REPLACE NOTEBOOK" in log and "MASK_EMAIL" in log
    assert (pkg / "snowflake/apps/notebook").exists()
    assert not [r for r in res if r.status == "FAILED"], res          # view succeeded on retry
    v = tgt.validate(pkg)
    assert any(r.kind == "table" and r.status == "OK" and "hash" in r.message for r in v)
    removes = [x for x in src.conn.log if x.upper().startswith("REMOVE")]
    assert removes and all("_migration_" in x for x in removes)      # never touches other @~ files
