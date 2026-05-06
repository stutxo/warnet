#!/usr/bin/env python3

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from warnet import deploy as deploy_module
from warnet.network_conditions import (
    TankEndpoint,
    build_rules_by_source,
    build_tc_commands,
    collect_addnode_edges,
    enable_netem_for_node,
    generate_latency_edges,
    network_conditions_enabled,
    parse_network_conditions,
)

NETWORK = {
    "networkConditions": {
        "enabled": True,
        "seed": 1337,
        "mode": "random-latency",
        "scope": "addnode-edges",
        "latency": {
            "minRttMs": 20,
            "maxRttMs": 600,
            "jitterPct": 10,
            "distribution": "normal",
        },
    },
    "nodes": [
        {"name": "tank-0000", "addnode": ["tank-0001", "tank-0002.default.svc:18444"]},
        {"name": "tank-0001", "addnode": ["tank-0000:18444"]},
        {"name": "tank-0002", "addnode": []},
    ],
}


class NetworkConditionsUnitTest(unittest.TestCase):
    def test_disabled_config_does_not_enable_netem(self):
        self.assertFalse(network_conditions_enabled({}))
        self.assertFalse(parse_network_conditions({}).enabled)

    def test_enable_netem_for_node_preserves_existing_values(self):
        node = {"name": "tank-0000", "netem": {"image": {"tag": "debug"}}}

        enabled = enable_netem_for_node(node)

        self.assertTrue(enabled["netem"]["enabled"])
        self.assertEqual(enabled["netem"]["image"]["tag"], "debug")
        self.assertNotIn("enabled", node["netem"])

    def test_addnode_edges_are_symmetric_and_skip_unknown_targets(self):
        network = {
            **NETWORK,
            "nodes": [
                *NETWORK["nodes"],
                {"name": "tank-0003", "addnode": ["example.com:18444"]},
            ],
        }
        with self.assertLogs("warnet.network_conditions", level="WARNING") as logs:
            edges = collect_addnode_edges(network)

        self.assertEqual(edges, [("tank-0000", "tank-0001"), ("tank-0000", "tank-0002")])
        self.assertIn("example.com:18444", "\n".join(logs.output))

    def test_seeded_latency_generation_is_reproducible(self):
        first = generate_latency_edges(NETWORK)
        second = generate_latency_edges(NETWORK)
        changed_seed = {
            **NETWORK,
            "networkConditions": {**NETWORK["networkConditions"], "seed": 7331},
        }

        self.assertEqual(first, second)
        self.assertNotEqual(first, generate_latency_edges(changed_seed))

    def test_rules_are_generated_for_both_directions(self):
        endpoints = {
            "tank-0000": TankEndpoint("tank-0000", "10.1.0.10", "10.96.0.10"),
            "tank-0001": TankEndpoint("tank-0001", "10.1.0.11", "10.96.0.11"),
            "tank-0002": TankEndpoint("tank-0002", "10.1.0.12", "10.96.0.12"),
        }

        rules = build_rules_by_source(generate_latency_edges(NETWORK), endpoints)

        self.assertEqual({rule.target for rule in rules["tank-0000"]}, {"tank-0001", "tank-0002"})
        self.assertEqual({rule.target for rule in rules["tank-0001"]}, {"tank-0000"})
        self.assertEqual({rule.target for rule in rules["tank-0002"]}, {"tank-0000"})
        self.assertEqual(rules["tank-0001"][0].target_ips, ("10.1.0.10", "10.96.0.10"))

    def test_tc_commands_shape_only_target_ips(self):
        endpoints = {
            "tank-0000": TankEndpoint("tank-0000", "10.1.0.10", "10.96.0.10"),
            "tank-0001": TankEndpoint("tank-0001", "10.1.0.11", "10.96.0.11"),
            "tank-0002": TankEndpoint("tank-0002", "10.1.0.12", "10.96.0.12"),
        }
        rules = build_rules_by_source(generate_latency_edges(NETWORK), endpoints)

        commands = "\n".join(build_tc_commands(rules["tank-0000"]))

        self.assertIn("tc qdisc del dev eth0 root || true", commands)
        self.assertIn("htb default 1", commands)
        self.assertIn("netem delay", commands)
        self.assertIn("distribution normal", commands)
        self.assertIn("match ip dst 10.1.0.11/32", commands)
        self.assertIn("match ip dst 10.96.0.11/32", commands)
        self.assertIn("match ip dst 10.1.0.12/32", commands)

    def test_deploy_wiring_only_runs_when_enabled(self):
        disabled_network = {"nodes": [{"name": "tank-0000"}]}
        enabled_network = {**NETWORK, "nodes": [{"name": "tank-0000", "addnode": []}]}

        disabled_nodes, disabled_apply = self.deploy_with_fakes(disabled_network)
        enabled_nodes, enabled_apply = self.deploy_with_fakes(enabled_network)

        self.assertNotIn("netem", disabled_nodes[0])
        disabled_apply.assert_not_called()
        self.assertTrue(enabled_nodes[0]["netem"]["enabled"])
        enabled_apply.assert_called_once()

    def deploy_with_fakes(self, network_file):
        deployed_nodes = []

        class FakeProcess:
            def __init__(self, target, args):
                self.args = args

            def start(self):
                deployed_nodes.append(self.args[0])

            def join(self):
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            (directory / "network.yaml").write_text(yaml.safe_dump(network_file))
            (directory / "node-defaults.yaml").write_text(yaml.safe_dump({"chain": "regtest"}))
            with (
                patch.object(deploy_module, "Process", FakeProcess),
                patch.object(deploy_module, "get_default_namespace_or", return_value="default"),
                patch.object(deploy_module, "apply_network_conditions") as apply_conditions,
            ):
                deploy_module.deploy_network(directory, namespace="default")

        return deployed_nodes, apply_conditions


class NetworkConditionsChartTest(unittest.TestCase):
    def setUp(self):
        self.helm = shutil.which("helm")
        if not self.helm:
            self.skipTest("helm is not installed")
        self.chart_dir = (
            Path(__file__).resolve().parents[1] / "resources" / "charts" / "bitcoincore"
        )

    def render_chart(self, values):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as values_file:
            yaml.safe_dump(values, values_file)
            values_file.flush()
            result = subprocess.run(
                [self.helm, "template", "tank-0000", str(self.chart_dir), "-f", values_file.name],
                check=True,
                capture_output=True,
                text=True,
            )
        return result.stdout

    def test_default_chart_renders_no_netem_sidecar(self):
        rendered = self.render_chart({})

        self.assertNotIn("name: netem", rendered)

    def test_enabled_chart_renders_netem_sidecar(self):
        rendered = self.render_chart({"netem": {"enabled": True}})

        self.assertIn("name: netem", rendered)
        self.assertIn("image: alpine:3.20", rendered)
        self.assertIn("NET_ADMIN", rendered)
        self.assertIn("NET_RAW", rendered)
        self.assertIn("apk add --no-cache iproute2 iputils", rendered)


if __name__ == "__main__":
    unittest.main()
