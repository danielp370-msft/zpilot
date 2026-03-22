"""Tests for zpilot.nodes module."""

import pytest
import tempfile
import os
from pathlib import Path

from zpilot.nodes import Node, NodeRegistry, load_nodes, LOCAL_NODE_NAME


class TestNode:
    def test_local_node(self):
        n = Node(name="local", transport_type="local")
        assert n.is_local
        assert n.transport is not None

    def test_transport_cached(self):
        n = Node(name="local", transport_type="local")
        t1 = n.transport
        t2 = n.transport
        assert t1 is t2  # same instance

    def test_ssh_node(self):
        n = Node(name="box1", transport_type="ssh", host="box1.example.com")
        assert not n.is_local
        assert n.host == "box1.example.com"


class TestNodeRegistry:
    def test_local_always_present(self):
        reg = NodeRegistry()
        assert "local" in reg
        assert len(reg) >= 1

    def test_get_unknown_raises(self):
        reg = NodeRegistry()
        with pytest.raises(KeyError, match="Unknown node"):
            reg.get("nonexistent")

    def test_add_remove(self):
        reg = NodeRegistry()
        n = Node(name="test1", transport_type="ssh", host="h1")
        reg.add(n)
        assert "test1" in reg
        assert reg.get("test1").host == "h1"
        reg.remove("test1")
        assert "test1" not in reg

    def test_cannot_remove_local(self):
        reg = NodeRegistry()
        with pytest.raises(ValueError, match="Cannot remove"):
            reg.remove("local")

    def test_names(self):
        reg = NodeRegistry([Node(name="local"), Node(name="a", host="h")])
        assert "local" in reg.names()
        assert "a" in reg.names()

    def test_remote_nodes(self):
        reg = NodeRegistry([
            Node(name="local"),
            Node(name="r1", transport_type="ssh", host="h1"),
        ])
        remote = reg.remote_nodes()
        assert len(remote) == 1
        assert remote[0].name == "r1"

    def test_all(self):
        reg = NodeRegistry([Node(name="local"), Node(name="r1", host="h")])
        assert len(reg.all()) == 2


class TestLoadNodes:
    def test_default_local_only(self):
        # With no config file, should return just local
        nodes = load_nodes()
        assert len(nodes) >= 1
        assert nodes[0].name == "local"

    def test_from_toml(self, tmp_path, monkeypatch):
        """Load nodes from a real TOML file."""
        nodes_file = tmp_path / "nodes.toml"
        nodes_file.write_text("""
[nodes.devbox1]
transport = "ssh"
host = "devbox1.internal"
user = "dan"

[nodes.wave2]
transport = "ssh"
host = "wave2"

[nodes.local]
# should be ignored — local is implicit
transport = "local"
""")
        import zpilot.nodes as nodes_mod
        monkeypatch.setattr(nodes_mod, "NODES_FILE", nodes_file)

        nodes = load_nodes()
        names = [n.name for n in nodes]
        assert "local" in names
        assert "devbox1" in names
        assert "wave2" in names
        # local should appear only once
        assert names.count("local") == 1

        devbox = next(n for n in nodes if n.name == "devbox1")
        assert devbox.host == "devbox1.internal"
        assert devbox.transport_opts.get("user") == "dan"
