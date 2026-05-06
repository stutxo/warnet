#!/usr/bin/env python3

import os
import re
import shutil
from pathlib import Path

import yaml
from test_base import TestBase

from warnet.network_conditions import generate_latency_edges
from warnet.process import run_command


class NetworkConditionsIntegrationTest(TestBase):
    def __init__(self):
        super().__init__()
        self.network_dir = Path(os.path.dirname(__file__)) / "data" / "network_conditions"

    def run_test(self):
        if not shutil.which("helm") or not shutil.which("kubectl"):
            self.network = False
            print("Skipping network conditions integration test: helm or kubectl missing")
            return

        try:
            self.setup_network()
            self.check_qdisc_rules()
            self.check_ping_latency()
        finally:
            self.cleanup()

    def setup_network(self):
        self.log.info("Setting up network with networkConditions")
        self.log.info(self.warnet(f"deploy {self.network_dir}"))
        self.wait_for_all_tanks_status(target="running")
        self.wait_for_all_edges()

    def check_qdisc_rules(self):
        for tank in ["tank-0000", "tank-0001", "tank-0002", "tank-0003"]:
            qdisc = run_command(
                f"kubectl exec {tank} -c netem --namespace default -- tc qdisc show dev eth0"
            )
            assert "htb" in qdisc, f"{tank} missing htb qdisc: {qdisc}"
            assert "netem" in qdisc, f"{tank} missing netem qdisc: {qdisc}"

    def check_ping_latency(self):
        with (self.network_dir / "network.yaml").open() as f:
            network_file = yaml.safe_load(f)
        edge = max(generate_latency_edges(network_file), key=lambda item: item.rtt_ms)

        target_ip = run_command(
            f"kubectl get pod {edge.node_b} --namespace default -o jsonpath='{{.status.podIP}}'"
        ).strip()
        ping = run_command(
            f"kubectl exec {edge.node_a} -c netem --namespace default -- ping -c 5 -W 3 {target_ip}"
        )
        avg_rtt = self.parse_ping_average_ms(ping)
        lower_bound = max(10, edge.rtt_ms * 0.5)
        upper_bound = edge.rtt_ms * 2 + 100

        assert lower_bound <= avg_rtt <= upper_bound, (
            f"Measured RTT {avg_rtt}ms outside expected range "
            f"{lower_bound}-{upper_bound}ms for {edge.node_a}->{edge.node_b} "
            f"generated RTT {edge.rtt_ms}ms\n{ping}"
        )

    @staticmethod
    def parse_ping_average_ms(output):
        match = re.search(r"(?:rtt|round-trip).* = [0-9.]+/([0-9.]+)", output)
        if not match:
            raise AssertionError(f"Could not parse ping RTT average:\n{output}")
        return float(match.group(1))


if __name__ == "__main__":
    test = NetworkConditionsIntegrationTest()
    test.run_test()
