"""Common building blocks every platform connector uses."""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

OK, FAILED, MANUAL, SKIPPED = "OK", "FAILED", "MANUAL", "SKIPPED"


class Friendly(Exception):
    """An error whose message is already plain language with a fix - shown to the user as is."""


@dataclass
class Resource:
    """Something found in an account (a table, a pipeline, a notebook...)."""
    kind: str
    name: str
    details: dict = field(default_factory=dict)


@dataclass
class Result:
    """Outcome of backing up / restoring / validating one item."""
    kind: str
    name: str
    status: str          # OK | FAILED | MANUAL | SKIPPED
    message: str = ""
    action: str = ""     # exact command(s) for THIS account, shown in a copy box


@dataclass
class Field:
    """One setting a connector needs. The app turns these into login forms automatically."""
    key: str                      # e.g. "ACCOUNT" -> env var SRC_SF_ACCOUNT / TGT_SF_ACCOUNT
    label: str
    secret: bool = False          # shown as a password box, stored in the OS credential manager
    default: str = ""
    help: str = ""
    options: tuple = ()           # non-empty -> dropdown
    sides: tuple = ("SRC", "TGT")
    advanced: bool = False        # hidden under "Advanced"
    required: bool = False


def safe_name(name: str) -> str:
    """Turn any object name into a safe file/folder name (Windows-safe too)."""
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", name) or "_"


class Connector(ABC):
    """
    One connector = one platform (Snowflake, Azure, Databricks, AWS, GCP ...).

    Settings come from environment variables / .env, named
        <SIDE>_<PREFIX>_<KEY>      e.g. SRC_SF_ACCOUNT, TGT_SF_ACCOUNT
    SIDE is SRC (old, expiring account) or TGT (new account).
    """
    name = "base"
    env_prefix = "BASE"
    title = "Base"
    fields: list = []             # list[Field] - drives the app's login forms

    @staticmethod
    def log(msg: str) -> None:    # replaced by the engine/app to show progress
        pass

    def __init__(self, side: str):
        if side not in ("SRC", "TGT"):
            raise ValueError("side must be SRC or TGT")
        self.side = side

    # ---- settings helpers -------------------------------------------------
    def var(self, key: str) -> str:
        return f"{self.side}_{self.env_prefix}_{key}"

    def env(self, key: str, default=None, required: bool = False):
        val = os.getenv(self.var(key), default)
        if required and not val:
            label = next((f.label for f in self.fields if f.key == key), key)
            which = "old" if self.side == "SRC" else "new"
            raise Friendly(f"Please fill in '{label}' for the {which} account (setting {self.var(key)}). "
                           f"Click 'How do I get these details?' to see where to find it.")
        return val

    def env_list(self, key: str) -> list[str]:
        return [x.strip() for x in (self.env(key, "") or "").split(",") if x.strip()]

    def apply_rewrites(self, text: str) -> str:
        """
        Replace old-account values with new ones, e.g. the old storage account URL.
        TGT_<PREFIX>_URL_REWRITE="oldacct.blob.core.windows.net=>newacct.blob.core.windows.net,old=>new"
        """
        for item in self.env_list("URL_REWRITE"):
            if "=>" in item:
                old, new = item.split("=>", 1)
                text = text.replace(old.strip(), new.strip())
        return text

    # ---- the five operations every platform implements -------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def discover(self) -> list[Resource]: ...

    @abstractmethod
    def backup(self, pkg: Path) -> list[Result]: ...

    @abstractmethod
    def restore(self, pkg: Path) -> list[Result]: ...

    @abstractmethod
    def validate(self, pkg: Path) -> list[Result]: ...

    def close(self) -> None:
        pass

    # ---- help for people using the tool for the first time --------------
    @classmethod
    def guide_text(cls) -> str:
        """Step-by-step 'where do I find these details' guide, from guides/<name>.md."""
        f = Path(__file__).resolve().parents[1] / "guides" / f"{cls.name}.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def explain(self, err: Exception) -> str:
        """Turn a raw error into what it means and what to do. Connectors add their own rules."""
        if isinstance(err, Friendly):
            return str(err)
        lines = [l.strip() for l in str(err).strip().splitlines() if l.strip()]
        return " ".join(lines[:3])[:400] if lines else type(err).__name__
