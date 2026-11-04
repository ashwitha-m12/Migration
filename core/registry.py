"""
Plugin registry. To support a new platform (AWS, GCP, BigQuery, Postgres ...):
  1. copy connectors/_template.py to connectors/<platform>_connector.py and fill it in
  2. add one line below
Nothing else in the tool has to change.
"""
import importlib
import os

PLUGINS = {
    "snowflake": "connectors.snowflake_connector:SnowflakeConnector",
    "azure": "connectors.azure_connector:AzureConnector",
    "databricks": "connectors.databricks_connector:DatabricksConnector",
}


def _extra():
    # CMT_EXTRA_PLUGINS="gcp=my_plugins.gcp:GcpConnector,aws=..."
    for item in os.getenv("CMT_EXTRA_PLUGINS", "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            PLUGINS.setdefault(k.strip(), v.strip())


def available() -> list[str]:
    _extra()
    return list(PLUGINS)


def load(platform: str):
    _extra()
    if platform not in PLUGINS:
        raise KeyError(f"Unknown platform '{platform}'. Available: {', '.join(PLUGINS)}")
    module, cls = PLUGINS[platform].split(":")
    return getattr(importlib.import_module(module), cls)
