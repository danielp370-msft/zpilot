"""Node registry — manages configured nodes and their transports."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .transport import Transport, create_transport

log = logging.getLogger("zpilot.nodes")

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


NODES_FILE = Path(
    os.environ.get("ZPILOT_NODES_FILE", "~/.config/zpilot/nodes.toml")
).expanduser()

LOCAL_NODE_NAME = "local"


@dataclass
class Node:
    """A zpilot node (local or remote machine running Zellij).

    Supported transports:
      - "local": runs commands directly on this machine
      - "ssh":   connects via SSH (requires host)
      - "mcp":   connects to a remote zpilot HTTP server (requires url/host + token)

    For MCP transport, configure in nodes.toml:
        [nodes.mynode]
        transport = "mcp"
        url = "https://zpilot-gh-8222.aue.devtunnels.ms"
        token = "shared-secret"
    """
    name: str
    transport_type: str = "local"  # local, ssh, mcp
    host: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    transport_opts: dict = field(default_factory=dict)
    # Runtime state (not persisted)
    _transport: Transport | None = field(default=None, repr=False)

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = create_transport(
                self.transport_type,
                host=self.host,
                **self.transport_opts,
            )
        return self._transport

    @property
    def is_local(self) -> bool:
        return self.transport_type == "local"


def load_nodes() -> list[Node]:
    """Load node definitions from nodes.toml.

    Always includes a 'local' node. Additional nodes come from the file.
    """
    nodes = [Node(name=LOCAL_NODE_NAME, transport_type="local")]

    if not NODES_FILE.exists():
        return nodes

    try:
        with open(NODES_FILE, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        log.warning(f"Failed to parse {NODES_FILE}: {e}")
        return nodes

    for name, cfg in data.get("nodes", {}).items():
        if name == LOCAL_NODE_NAME:
            continue  # local is always implicit
        transport_type = cfg.get("transport", "ssh")
        host = cfg.get("host") or cfg.get("url")  # url for mcp transport
        labels = cfg.get("labels", {})
        # Everything else goes into transport_opts
        transport_opts = {
            k: v for k, v in cfg.items()
            if k not in ("transport", "host", "labels", "token_file")
        }

        # Resolve token_file if present (replaces inline plaintext token)
        token_file = cfg.get("token_file", "")
        if token_file:
            from .security import load_token
            loaded = load_token(token_file)
            if loaded:
                transport_opts["token"] = loaded
            else:
                log.warning(f"Node {name}: could not read token from {token_file}")
        elif "token" in transport_opts and transport_opts["token"].startswith("file:"):
            from .security import load_token
            loaded = load_token(transport_opts["token"][5:])
            if loaded:
                transport_opts["token"] = loaded
            else:
                log.warning(f"Node {name}: could not read token from file ref")

        nodes.append(Node(
            name=name,
            transport_type=transport_type,
            host=host,
            labels=labels,
            transport_opts=transport_opts,
        ))

    return nodes


class NodeRegistry:
    """Registry of known nodes. Thread-safe lookup by name."""

    def __init__(self, nodes: list[Node] | None = None):
        self._nodes: dict[str, Node] = {}
        for node in (nodes or load_nodes()):
            self._nodes[node.name] = node

    def get(self, name: str) -> Node:
        """Get a node by name. Raises KeyError if not found."""
        if name not in self._nodes:
            available = ", ".join(sorted(self._nodes.keys()))
            raise KeyError(f"Unknown node '{name}'. Available: {available}")
        return self._nodes[name]

    def all(self) -> list[Node]:
        """All registered nodes."""
        return list(self._nodes.values())

    def names(self) -> list[str]:
        return list(self._nodes.keys())

    def remote_nodes(self) -> list[Node]:
        """Non-local nodes only."""
        return [n for n in self._nodes.values() if not n.is_local]

    def add(self, node: Node) -> None:
        self._nodes[node.name] = node

    def remove(self, name: str) -> None:
        if name == LOCAL_NODE_NAME:
            raise ValueError("Cannot remove the local node")
        self._nodes.pop(name, None)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, name: str) -> bool:
        return name in self._nodes
