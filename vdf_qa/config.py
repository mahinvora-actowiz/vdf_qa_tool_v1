"""Runtime settings, read from environment variables with safe defaults."""
from __future__ import annotations

import os
from dotenv import load_dotenv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(name: str) -> Optional[int]:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw else None


@dataclass(frozen=True)
class Settings:
    mongo_uri: str
    mongo_database: str
    input_dir: str                  # the API only reads data files from this folder
    schema_dir: str                 # client schema files (*.json)
    entry_schema: str               # schema each record is validated against
    reject_on_schema_error: bool    # schema-invalid records reject the file
    max_flag_hits: Optional[int]    # optional cap on High-priority flag hits (None = no cap)
    api_key: Optional[str]

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        load_dotenv(project_root / ".env", override=True)
        input_dir = os.environ.get("VDF_INPUT_DIR", "./inbox")
        Path(input_dir).mkdir(parents=True, exist_ok=True)
        return cls(
            mongo_uri=os.environ.get("VDF_MONGODB_URI", "mongodb://localhost:27017"),
            mongo_database=os.environ.get("VDF_MONGODB_DATABASE", "Ics_Auto_QATool"),
            input_dir=input_dir,
            schema_dir=os.environ.get("VDF_SCHEMA_DIR", "./schemas"),
            entry_schema=os.environ.get("VDF_ENTRY_SCHEMA", "TransformVendorProduct.schema.json"),
            reject_on_schema_error=_bool("VDF_REJECT_ON_SCHEMA_ERROR", True),
            max_flag_hits=_optional_int("VDF_MAX_FLAG_HITS"),
            api_key=os.environ.get("VDF_API_KEY") or None,
        )
