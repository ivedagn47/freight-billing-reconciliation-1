from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUILTIN_VARS = {"_run_id", "_run_dir", "_run_artefact_dir", "_flow_dir", "_node_id"}
AGENT_BUILTIN_VARS = {"_session_id"}
VAR_TYPES = {"string", "path", "number", "integer", "boolean", "dict", "list", "any"}
PHASE3_RUNNERS = {"fork", "join", "dynamic_fanout"}


@dataclass
class OutputFile:
    name: str
    path: str  # template
    definition: str
    schema_path: Path
    schema: dict


@dataclass
class VarBinding:
    var: str
    file: str
    pointer: str | None = None  # None: the variable is the file's path


@dataclass
class OutputSchema:
    name: str
    files: list[OutputFile]
    bindings: list[VarBinding]

    def produced_vars(self) -> set[str]:
        return {b.var for b in self.bindings}


@dataclass
class VariableDecl:
    name: str
    type: str = "string"
    required: bool = True
    has_default: bool = False
    default: Any = None
    description: str | None = None


@dataclass
class Node:
    id: str
    kind: str  # start | done | agent | script | fork | join | dynamic_fanout
    attrs: dict[str, str]
    description: str | None = None
    prompt_template: Path | None = None
    script: Path | None = None
    working_dir: str | None = None
    output_schema: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    pause_at: str = "never"
    max_retries: int | None = None
    timeout: float | None = None
    stall_after: float | None = None
    max_budget_usd: float | None = None
    harness: str | None = None
    add_dirs: list[str] = field(default_factory=list)


@dataclass
class Edge:
    source: str
    target: str
    attrs: dict[str, str]
    gates: list[Path] = field(default_factory=list)
    condition: str | None = None
    condition_tree: Any = None

    @property
    def id(self) -> str:
        return f"{self.source}->{self.target}"


@dataclass
class Flow:
    name: str
    dir: Path
    dot_path: Path
    yml_path: Path
    graph_name: str
    label: str | None
    nodes: dict[str, Node]
    edges: list[Edge]
    variables: dict[str, VariableDecl]
    output_schemas: dict[str, OutputSchema]
    start: str
    done: str
    digest: str
    input_vars: set[str] = field(default_factory=set)

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.target == node_id]

    def produced_vars(self, node_id: str) -> set[str]:
        node = self.nodes[node_id]
        produced = set()
        if node.output_schema and node.output_schema in self.output_schemas:
            produced |= self.output_schemas[node.output_schema].produced_vars()
        if node.kind == "join" and node.attrs.get("summary_var"):
            produced.add(node.attrs["summary_var"])
        return produced
