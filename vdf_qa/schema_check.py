"""JSON Schema (draft-07) validation for the client schema set.

The client publishes several related schema files (ScrapeVendorProduct,
TransformVendorProduct, AssetIndex). They are loaded from one directory
into a referencing Registry so that cross-file $ref values resolve offline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7


@dataclass(frozen=True)
class SchemaIssue:
    path: str
    message: str


def load_registry(schema_dir: str) -> Registry:
    resources = []
    # Every .json file in the folder is a schema. The uploaded file names differ from the
    # usual *.schema.json pattern, so the pattern is not required.
    for p in sorted(Path(schema_dir).glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        uri = doc.get("$id") or p.name
        resources.append((uri, Resource.from_contents(doc, default_specification=DRAFT7)))
    return Registry().with_resources(resources)


class SchemaChecker:
    def __init__(self, schema_dir: str, entry_schema: str, max_errors_per_record: int = 5):
        registry = load_registry(schema_dir)
        schema_path = Path(schema_dir) / entry_schema
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft7Validator.check_schema(schema)
        self._validator = Draft7Validator(schema, registry=registry)
        self._max = max_errors_per_record

    def check(self, record) -> list[SchemaIssue]:
        out: list[SchemaIssue] = []
        errors = sorted(self._validator.iter_errors(record), key=lambda e: list(e.absolute_path))
        for err in errors[: self._max]:
            path = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/"
            out.append(SchemaIssue(path=path, message=err.message))
        return out
