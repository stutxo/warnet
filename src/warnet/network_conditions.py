import ipaddress
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from time import sleep, time
from typing import Any, Optional

from kubernetes.stream import stream

from .k8s import get_default_namespace_or, get_static_client

LOGGER = logging.getLogger(__name__)
NETEM_CONTAINER = "netem"
DEFAULT_INTERFACE = "eth0"
DEFAULT_MIN_RTT_MS = 20
DEFAULT_MAX_RTT_MS = 600
DEFAULT_JITTER_PCT = 10
DEFAULT_DISTRIBUTION = "normal"
SUPPORTED_DISTRIBUTIONS = {"normal", "pareto", "paretonormal", "uniform"}


@dataclass(frozen=True)
class LatencyProfile:
    min_rtt_ms: int = DEFAULT_MIN_RTT_MS
    max_rtt_ms: int = DEFAULT_MAX_RTT_MS
    jitter_pct: int = DEFAULT_JITTER_PCT
    distribution: str = DEFAULT_DISTRIBUTION


@dataclass(frozen=True)
class NetworkConditions:
    enabled: bool = False
    seed: int = 0
    mode: str = "random-latency"
    scope: str = "addnode-edges"
    latency: LatencyProfile = LatencyProfile()


@dataclass(frozen=True)
class LatencyEdge:
    node_a: str
    node_b: str
    rtt_ms: int
    one_way_delay_ms: int
    jitter_ms: int
    distribution: str


@dataclass(frozen=True)
class TankEndpoint:
    name: str
    pod_ip: str
    service_ip: str


@dataclass(frozen=True)
class NetemRule:
    target: str
    target_ips: tuple[str, ...]
    one_way_delay_ms: int
    jitter_ms: int
    distribution: str


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


def parse_network_conditions(network_file: Mapping[str, Any]) -> NetworkConditions:
    raw = network_file.get("networkConditions") or {}
    if not raw:
        return NetworkConditions()
    if not isinstance(raw, Mapping):
        raise ValueError("networkConditions must be a mapping")

    enabled = bool(raw.get("enabled", False))
    if not enabled:
        return NetworkConditions(enabled=False)

    mode = raw.get("mode", "random-latency")
    if mode != "random-latency":
        raise ValueError("networkConditions.mode must be 'random-latency'")

    scope = raw.get("scope", "addnode-edges")
    if scope != "addnode-edges":
        raise ValueError("networkConditions.scope must be 'addnode-edges'")

    seed = _positive_or_zero_int(raw.get("seed", 0), "networkConditions.seed")
    latency_raw = raw.get("latency") or {}
    if not isinstance(latency_raw, Mapping):
        raise ValueError("networkConditions.latency must be a mapping")

    min_rtt_ms = _positive_int(
        latency_raw.get("minRttMs", DEFAULT_MIN_RTT_MS),
        "networkConditions.latency.minRttMs",
    )
    max_rtt_ms = _positive_int(
        latency_raw.get("maxRttMs", DEFAULT_MAX_RTT_MS),
        "networkConditions.latency.maxRttMs",
    )
    if min_rtt_ms > max_rtt_ms:
        raise ValueError("networkConditions.latency.minRttMs cannot exceed maxRttMs")

    jitter_pct = _positive_or_zero_int(
        latency_raw.get("jitterPct", DEFAULT_JITTER_PCT),
        "networkConditions.latency.jitterPct",
    )
    if jitter_pct > 100:
        raise ValueError("networkConditions.latency.jitterPct cannot exceed 100")

    distribution = latency_raw.get("distribution", DEFAULT_DISTRIBUTION)
    if distribution not in SUPPORTED_DISTRIBUTIONS:
        raise ValueError(
            "networkConditions.latency.distribution must be one of "
            + ", ".join(sorted(SUPPORTED_DISTRIBUTIONS))
        )

    return NetworkConditions(
        enabled=True,
        seed=seed,
        mode=mode,
        scope=scope,
        latency=LatencyProfile(
            min_rtt_ms=min_rtt_ms,
            max_rtt_ms=max_rtt_ms,
            jitter_pct=jitter_pct,
            distribution=distribution,
        ),
    )


def network_conditions_enabled(network_file: Mapping[str, Any]) -> bool:
    return parse_network_conditions(network_file).enabled


def enable_netem_for_node(node: Mapping[str, Any]) -> dict[str, Any]:
    node_values = dict(node)
    netem = dict(node_values.get("netem") or {})
    netem["enabled"] = True
    node_values["netem"] = netem
    return node_values


def generate_latency_edges(network_file: Mapping[str, Any]) -> list[LatencyEdge]:
    conditions = parse_network_conditions(network_file)
    if not conditions.enabled:
        return []

    edges = collect_addnode_edges(network_file)
    rng = random.Random(conditions.seed)
    latency_edges = []
    for node_a, node_b in edges:
        rtt_ms = rng.randint(
            conditions.latency.min_rtt_ms,
            conditions.latency.max_rtt_ms,
        )
        one_way_delay_ms = max(1, round(rtt_ms / 2))
        jitter_ms = round(one_way_delay_ms * conditions.latency.jitter_pct / 100)
        latency_edges.append(
            LatencyEdge(
                node_a=node_a,
                node_b=node_b,
                rtt_ms=rtt_ms,
                one_way_delay_ms=one_way_delay_ms,
                jitter_ms=jitter_ms,
                distribution=conditions.latency.distribution,
            )
        )
    return latency_edges


def collect_addnode_edges(network_file: Mapping[str, Any]) -> list[tuple[str, str]]:
    nodes = network_file.get("nodes") or []
    if not isinstance(nodes, list):
        raise ValueError("network.yaml nodes must be a list")

    tank_names = {node.get("name") for node in nodes if isinstance(node, Mapping)}
    tank_names.discard(None)

    edges = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        source = node.get("name")
        if not source:
            continue
        for addnode in _as_list(node.get("addnode")):
            target = _normalize_tank_target(addnode, tank_names)
            if target is None:
                LOGGER.warning("Skipping non-tank addnode target from %s: %s", source, addnode)
                continue
            if target == source:
                LOGGER.warning("Skipping self addnode target on %s: %s", source, addnode)
                continue
            edges.add(tuple(sorted((source, target))))

    return sorted(edges)


def build_rules_by_source(
    edges: list[LatencyEdge],
    endpoints: Mapping[str, TankEndpoint],
) -> dict[str, list[NetemRule]]:
    rules: dict[str, list[NetemRule]] = {name: [] for name in endpoints}
    for edge in edges:
        for source, target in ((edge.node_a, edge.node_b), (edge.node_b, edge.node_a)):
            endpoint = endpoints[target]
            target_ips = _dedupe_ips((endpoint.pod_ip, endpoint.service_ip))
            rules.setdefault(source, []).append(
                NetemRule(
                    target=target,
                    target_ips=target_ips,
                    one_way_delay_ms=edge.one_way_delay_ms,
                    jitter_ms=edge.jitter_ms,
                    distribution=edge.distribution,
                )
            )
    return rules


def build_tc_commands(rules: list[NetemRule], interface: str = DEFAULT_INTERFACE) -> list[str]:
    commands = [f"tc qdisc del dev {interface} root || true"]
    if not rules:
        return commands

    commands.extend(
        [
            f"tc qdisc add dev {interface} root handle 1: htb default 1",
            f"tc class add dev {interface} parent 1: classid 1:1 htb rate 10000mbit ceil 10000mbit",
        ]
    )

    filter_prio = 10
    for index, rule in enumerate(rules, start=10):
        class_id = f"1:{index}"
        commands.append(
            f"tc class add dev {interface} parent 1: classid {class_id} "
            "htb rate 10000mbit ceil 10000mbit"
        )
        commands.append(
            f"tc qdisc add dev {interface} parent {class_id} handle {index}: "
            f"netem {format_netem_delay(rule)}"
        )
        for ip in rule.target_ips:
            commands.append(
                f"tc filter add dev {interface} protocol ip parent 1: prio {filter_prio} "
                f"u32 match ip dst {ip}/32 flowid {class_id}"
            )
            filter_prio += 1

    return commands


def format_netem_delay(rule: NetemRule) -> str:
    delay = f"delay {rule.one_way_delay_ms}ms"
    if rule.jitter_ms <= 0:
        return delay
    return f"{delay} {rule.jitter_ms}ms distribution {rule.distribution}"


def apply_network_conditions(
    network_file: Mapping[str, Any],
    namespace: Optional[str] = None,
    interface: str = DEFAULT_INTERFACE,
    timeout: int = 300,
) -> list[LatencyEdge]:
    conditions = parse_network_conditions(network_file)
    if not conditions.enabled:
        return []

    namespace = get_default_namespace_or(namespace)
    edges = generate_latency_edges(network_file)
    tank_names = _network_tank_names(network_file)
    if not tank_names:
        LOGGER.warning("networkConditions enabled but no tanks were found")
        return []

    client = get_static_client()
    endpoints = {
        name: _wait_for_netem_endpoint(client, namespace, name, timeout=timeout)
        for name in tank_names
    }
    rules_by_source = build_rules_by_source(edges, endpoints)

    for source in tank_names:
        rules = rules_by_source.get(source, [])
        commands = build_tc_commands(rules, interface=interface)
        for command in commands:
            _exec_netem(client, namespace, source, command)
        LOGGER.info("Applied %d netem rule(s) to %s", len(rules), source)

    return edges


def _network_tank_names(network_file: Mapping[str, Any]) -> list[str]:
    nodes = network_file.get("nodes") or []
    return sorted(
        node["name"]
        for node in nodes
        if isinstance(node, Mapping) and isinstance(node.get("name"), str)
    )


def _wait_for_netem_endpoint(client, namespace: str, pod_name: str, timeout: int) -> TankEndpoint:
    deadline = time() + timeout
    last_error = None
    while time() < deadline:
        try:
            pod = client.read_namespaced_pod(name=pod_name, namespace=namespace)
            service = client.read_namespaced_service(name=pod_name, namespace=namespace)
            pod_ip = pod.status.pod_ip
            service_ip = service.spec.cluster_ip
            statuses = {status.name: status for status in pod.status.container_statuses or []}
            netem_status = statuses.get(NETEM_CONTAINER)
            if (
                pod.status.phase == "Running"
                and pod_ip
                and service_ip
                and netem_status
                and netem_status.state
                and netem_status.state.running
            ):
                ready = _exec_netem(
                    client,
                    namespace,
                    pod_name,
                    "command -v tc >/dev/null && command -v ping >/dev/null",
                    check=False,
                )
                if ready.returncode == 0:
                    return TankEndpoint(name=pod_name, pod_ip=pod_ip, service_ip=service_ip)
        except Exception as err:
            last_error = err
        sleep(1)

    raise TimeoutError(f"Timed out waiting for netem sidecar in {pod_name}: {last_error}")


def _exec_netem(
    client,
    namespace: str,
    pod_name: str,
    shell_command: str,
    check: bool = True,
) -> ExecResult:
    marker = "__WARNET_EXIT_CODE__"
    wrapped = f"{shell_command}; rc=$?; printf '\\n{marker}%s\\n' \"$rc\""
    resp = stream(
        client.connect_get_namespaced_pod_exec,
        name=pod_name,
        namespace=namespace,
        container=NETEM_CONTAINER,
        command=["sh", "-c", wrapped],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )

    stdout = ""
    stderr = ""
    while resp.is_open():
        resp.update(timeout=5)
        if resp.peek_stdout():
            stdout += resp.read_stdout()
        if resp.peek_stderr():
            stderr += resp.read_stderr()
    resp.close()

    returncode = 0
    clean_stdout_lines = []
    for line in stdout.splitlines():
        if line.startswith(marker):
            returncode = int(line.removeprefix(marker))
        else:
            clean_stdout_lines.append(line)
    result = ExecResult(returncode=returncode, stdout="\n".join(clean_stdout_lines), stderr=stderr)

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed in {pod_name}/{NETEM_CONTAINER}: {shell_command}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _normalize_tank_target(value: Any, tank_names: set[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    target = value.strip()
    if not target or target.startswith("["):
        return None

    host = target.split("/", 1)[0]
    if ":" in host:
        host = host.split(":", 1)[0]
    if "." in host:
        host = host.split(".", 1)[0]

    return host if host in tank_names else None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _dedupe_ips(values: tuple[str, ...]) -> tuple[str, ...]:
    ips = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        ipaddress.ip_address(value)
        seen.add(value)
        ips.append(value)
    return tuple(ips)


def _positive_int(value: Any, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_or_zero_int(value: Any, name: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} cannot be negative")
    return parsed
