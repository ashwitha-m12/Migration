"""
TEMPLATE - copy this file to add any new platform (AWS, GCP, BigQuery, Postgres, MongoDB ...).

1. cp connectors/_template.py connectors/gcp_connector.py
2. rename the class, set `name` and `env_prefix`
3. fill in the five methods using that platform's Python SDK
4. add one line to core/registry.py:  "gcp": "connectors.gcp_connector:GcpConnector"

The rules every connector follows:
  * discover()  -> list what exists in the OLD account (read-only)
  * backup(pkg) -> write everything under pkg/<platform>/ ; never write secrets
  * restore(pkg)-> read ONLY from pkg (old account may already be gone), create in dependency order
  * validate()  -> compare the NEW account against what the backup says should exist
  * report anything you cannot automate as Result(..., MANUAL, "what the user must do")

Examples of what to back up per platform:
  AWS : S3 buckets (boto3), Glue jobs/crawlers, Lambda code, Redshift DDL + UNLOAD, IAM policies (as templates)
  GCP : GCS buckets (google-cloud-storage), BigQuery datasets (schemas + EXPORT DATA), Cloud Functions source
  Postgres/MySQL: pg_dump / mysqldump
"""
from pathlib import Path

from core.base import MANUAL, OK, Connector, Field, Resource, Result


class TemplateConnector(Connector):
    name = "template"
    env_prefix = "TPL"            # settings will be SRC_TPL_* and TGT_TPL_*
    title = "Template"
    fields = [                    # the app builds the login form from this list
        Field("ACCESS_KEY", "Access key", required=True),
        Field("SECRET_KEY", "Secret key", secret=True, required=True),
    ]

    def connect(self):
        self.client = None        # e.g. boto3.client("s3", aws_access_key_id=self.env("ACCESS_KEY", required=True), ...)

    def discover(self) -> list[Resource]:
        return []

    def backup(self, pkg: Path) -> list[Result]:
        (pkg / self.name).mkdir(parents=True, exist_ok=True)
        return [Result("example", "nothing yet", MANUAL, "implement backup()")]

    def restore(self, pkg: Path) -> list[Result]:
        return []

    def validate(self, pkg: Path) -> list[Result]:
        return [Result("example", "nothing yet", OK, "")]
