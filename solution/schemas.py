"""Schema registry for workflows and disposition catalogue."""

import csv
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def normalize_header(s: str) -> str:
    """Normalize a column header or alias: lowercase, trim, collapse whitespace and underscores."""
    return re.sub(r"[\s_]+", " ", s.lower().strip())


@dataclass
class FieldSchema:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: List[str] = field(default_factory=list)
    default: Optional[str] = None
    aliases: List[str] = field(default_factory=list)


@dataclass
class WorkflowSchema:
    workflow_id: str
    description: str
    fields: List[FieldSchema]
    field_by_name: Dict[str, FieldSchema] = field(default_factory=dict)
    header_map: Dict[str, str] = field(default_factory=dict)
    reference_field: Optional[str] = None

    def __post_init__(self):
        self.field_by_name = {f.name: f for f in self.fields}
        self.header_map = {}
        for f in self.fields:
            if f.reference:
                self.reference_field = f.name
            # Map canonical name
            self.header_map[normalize_header(f.name)] = f.name
            # Map aliases
            for alias in f.aliases:
                self.header_map[normalize_header(alias)] = f.name


@dataclass
class DispositionInfo:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


class SchemaRegistry:
    def __init__(self, schemas_dir: str):
        self.schemas_dir = schemas_dir
        self.workflows: Dict[str, WorkflowSchema] = {}
        self.dispositions: Dict[tuple[str, str], DispositionInfo] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.schemas_dir):
            raise FileNotFoundError(f"Schemas directory not found: {self.schemas_dir}")

        for fname in sorted(os.listdir(self.schemas_dir)):
            full_path = os.path.join(self.schemas_dir, fname)
            if fname.endswith(".json") and os.path.isfile(full_path):
                with open(full_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                wf_id = data["workflow_id"]
                fields = []
                for fd in data.get("fields", []):
                    fields.append(
                        FieldSchema(
                            name=fd["name"],
                            type=fd["type"],
                            required=fd.get("required", False),
                            pii=fd.get("pii", False),
                            reference=fd.get("reference", False),
                            values=fd.get("values", []),
                            default=fd.get("default"),
                            aliases=fd.get("aliases", []),
                        )
                    )
                self.workflows[wf_id] = WorkflowSchema(
                    workflow_id=wf_id,
                    description=data.get("description", ""),
                    fields=fields,
                )

        disp_path = os.path.join(self.schemas_dir, "dispositions.csv")
        if os.path.exists(disp_path):
            with open(disp_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wf = row["workflow_id"].strip()
                    code = row["code"].strip()
                    label = row["label"].strip()
                    is_term = row["is_terminal"].strip().lower() == "true"
                    is_succ = row["is_success"].strip().lower() == "true"
                    self.dispositions[(wf, code)] = DispositionInfo(
                        workflow_id=wf,
                        code=code,
                        label=label,
                        is_terminal=is_term,
                        is_success=is_succ,
                    )

    def get_workflow(self, workflow_id: str) -> Optional[WorkflowSchema]:
        return self.workflows.get(workflow_id)

    def get_disposition(
        self, workflow_id: str, code: str
    ) -> Optional[DispositionInfo]:
        return self.dispositions.get((workflow_id, code))

    def is_known_disposition(self, workflow_id: str, code: str) -> bool:
        return (workflow_id, code) in self.dispositions
