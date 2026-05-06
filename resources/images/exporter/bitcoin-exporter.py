import hashlib
import json
import os

from authproxy import AuthServiceProxy
from prometheus_client import Gauge, start_http_server
from prometheus_client.core import GaugeMetricFamily, REGISTRY


# Ensure that all RPC calls are made with brand new http connections
def auth_proxy_request(self, method, path, postdata):
    self._set_conn()  # creates new http client connection
    return self.oldrequest(method, path, postdata)


AuthServiceProxy.oldrequest = AuthServiceProxy._request
AuthServiceProxy._request = auth_proxy_request


# RPC Credentials for bitcoin node
# By default we assume the container is in the same pod as bitcoind, on regtest
BITCOIN_RPC_HOST = os.environ.get("BITCOIN_RPC_HOST", "localhost")
BITCOIN_RPC_PORT = os.environ.get("BITCOIN_RPC_PORT", "18443")
BITCOIN_RPC_USER = os.environ.get("BITCOIN_RPC_USER", "warnet_user")
BITCOIN_RPC_PASSWORD = os.environ.get("BITCOIN_RPC_PASSWORD", "2themoon")

DEFAULT_METRICS = (
    'blocks=getblockcount() '
    'inbounds=getnetworkinfo()["connections_in"] '
    'outbounds=getnetworkinfo()["connections_out"] '
    'mempool_size=getmempoolinfo()["size"]'
)

NAN = float("nan")
DEFAULT_DECISION_HISTORY_LIMIT = 20
CHAR_RESOLUTION_VERBOSITY = 1


def metric_float(value):
    if value is None:
        return NAN
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return float(value)


def metric_fingerprint(value):
    if value is None:
        return NAN
    if isinstance(value, (dict, list, tuple)):
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    elif isinstance(value, bytes):
        serialized = value.hex()
    else:
        serialized = str(value)

    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    # Prometheus stores float64 samples. Keep the fingerprint under 2^53 so it
    # remains exact while still giving enough bits to spot disagreements.
    return float(int(digest[:12], 16))


def evaluate_rpc(rpc, cmd):
    return eval(f"rpc.{cmd}", {"rpc": rpc})


def safe_metric(label, func):
    def wrapped():
        try:
            return func()
        except Exception as e:
            print(f"Metric {label} failed: {e}", flush=True)
            return NAN

    return wrapped


def make_metric_function(rpc, cmd):
    return lambda: metric_float(evaluate_rpc(rpc, cmd))


def make_hash_function(rpc, cmd):
    return lambda: metric_fingerprint(evaluate_rpc(rpc, cmd))


def make_counting_function(rpc, cmd, key, value):
    return lambda: float(
        sum(
            1
            for item in evaluate_rpc(rpc, cmd)
            if item.get(key) == value or str(item.get(key)) == value
        )
    )


def domain_is_scheduled(rpc, domain_info):
    return any(entry.get("info") == domain_info for entry in rpc.domain_registry("list"))


def label_value(value):
    if value is None:
        return ""
    return str(value)


def bool_label(value):
    return "true" if value else "false"


def hex_text_label(value):
    if not value:
        return ""
    try:
        return bytes.fromhex(str(value)).decode("utf-8")
    except Exception:
        return ""


def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decision_roll_labels(entry, fallback_ballot=None):
    if not isinstance(entry, dict):
        entry = {}
    if not entry:
        labels = {
            "found": "",
            "resolution_type": "",
            "roll_hash": "",
            "data_hash": "",
            "data": "",
            "data_text": "",
        }
        if fallback_ballot is not None:
            labels["ballot"] = label_value(fallback_ballot)
        return labels

    if entry.get("resolution_type") == "impossible":
        roll = entry.get("impossible_roll") or {}
    else:
        roll = entry.get("decision_roll") or {}
    if not isinstance(roll, dict):
        roll = {}

    data = roll.get("data") if entry.get("resolution_type") != "impossible" else ""
    labels = {
        "found": bool_label(entry.get("found")),
        "resolution_type": label_value(entry.get("resolution_type")),
        "roll_hash": label_value(roll.get("roll_hash")),
        "data_hash": label_value(roll.get("data_hash")),
        "data": label_value(data),
        "data_text": hex_text_label(data),
    }

    if fallback_ballot is not None:
        labels["ballot"] = label_value(
            entry.get("ballot_number", fallback_ballot)
        )

    return labels


def latest_decision_roll_labels(rpc, domain, latest_ballot):
    latest_ballot = parse_int(latest_ballot)
    if latest_ballot is None:
        return decision_roll_labels({})

    try:
        resolutions = rpc.getreferendumresolution(
            domain,
            latest_ballot,
            latest_ballot,
            CHAR_RESOLUTION_VERBOSITY,
        )
    except Exception:
        return decision_roll_labels({})

    if isinstance(resolutions, dict):
        resolutions = resolutions.get("resolutions", [])
    if not isinstance(resolutions, list) or not resolutions:
        return decision_roll_labels({})

    return decision_roll_labels(resolutions[0])


def make_char_domain_function(rpc, domain, domain_info, key, *, fingerprint=False):
    def char_domain_metric():
        if not domain_is_scheduled(rpc, domain_info):
            return NAN

        value = rpc.getdomaininfo(domain)[key]
        if fingerprint:
            return metric_fingerprint(value)
        return metric_float(value)

    return char_domain_metric


class CharDomainInfoCollector:
    def __init__(self, rpc, label, domain, domain_info):
        self.rpc = rpc
        self.label = label
        self.domain = domain
        self.domain_info = domain_info

    def collect(self):
        try:
            if not domain_is_scheduled(self.rpc, self.domain_info):
                return

            try:
                info = self.rpc.getdomaininfo(self.domain)
            except Exception:
                info = {}

            latest_decided_ballot = info.get("latest_decided_ballot")
            latest_decision = latest_decision_roll_labels(
                self.rpc,
                self.domain,
                latest_decided_ballot,
            )
            metric = GaugeMetricFamily(
                self.label,
                f"CHAR_DOMAIN_INFO:{self.domain},{self.domain_info}",
                labels=[
                    "domain",
                    "domain_info",
                    "next_ballot",
                    "next_leader_bond",
                    "is_next_leader_mine",
                    "latest_decided_ballot",
                    "latest_decision_roll_hash",
                    "latest_decision_data_hash",
                    "latest_decision_data",
                    "latest_decision_data_text",
                    "latest_decision_zeitgeist",
                    "latest_decision_found",
                    "latest_decision_resolution_type",
                ],
            )
            metric.add_metric(
                [
                    self.domain,
                    self.domain_info,
                    label_value(info.get("next_ballot")),
                    label_value(info.get("next_leader_bond")),
                    bool_label(info.get("is_next_leader_mine")),
                    label_value(latest_decided_ballot),
                    label_value(
                        info.get("latest_decision_roll_hash")
                        or latest_decision["roll_hash"]
                    ),
                    label_value(
                        info.get("latest_decision_data_hash")
                        or latest_decision["data_hash"]
                    ),
                    latest_decision["data"],
                    latest_decision["data_text"],
                    label_value(info.get("latest_decision_zeitgeist")),
                    latest_decision["found"],
                    latest_decision["resolution_type"],
                ],
                1.0,
            )
            yield metric
        except Exception as e:
            print(f"Metric {self.label} failed: {e}", flush=True)


def decision_roll_history(rpc, domain, latest_ballot, limit):
    latest_ballot = parse_int(latest_ballot)
    if latest_ballot is None or limit <= 0:
        return []

    first_ballot = max(0, latest_ballot - limit + 1)
    resolutions = rpc.getreferendumresolution(
        domain,
        first_ballot,
        latest_ballot,
        CHAR_RESOLUTION_VERBOSITY,
    )
    if isinstance(resolutions, dict):
        resolutions = resolutions.get("resolutions", [])
    if not isinstance(resolutions, list):
        return []

    history = []
    for offset, entry in enumerate(resolutions):
        if not isinstance(entry, dict):
            continue

        history.append(decision_roll_labels(entry, first_ballot + offset))

    return sorted(
        history,
        key=lambda row: parse_int(row["ballot"]) or -1,
        reverse=True,
    )[:limit]


class CharDomainDecisionHistoryCollector:
    def __init__(self, rpc, label, domain, domain_info, limit):
        self.rpc = rpc
        self.label = label
        self.domain = domain
        self.domain_info = domain_info
        self.limit = parse_int(limit)
        if self.limit is None:
            self.limit = DEFAULT_DECISION_HISTORY_LIMIT

    def collect(self):
        try:
            if not domain_is_scheduled(self.rpc, self.domain_info):
                return

            try:
                info = self.rpc.getdomaininfo(self.domain)
                latest_decided_ballot = info.get("latest_decided_ballot")
            except Exception:
                latest_decided_ballot = None

            metric = GaugeMetricFamily(
                self.label,
                f"CHAR_DOMAIN_DECISION_HISTORY:{self.domain},{self.domain_info},{self.limit}",
                labels=[
                    "domain",
                    "domain_info",
                    "history_index",
                    "ballot",
                    "found",
                    "resolution_type",
                    "roll_hash",
                    "data_hash",
                    "data",
                    "data_text",
                ],
            )
            for index, entry in enumerate(
                decision_roll_history(
                    self.rpc,
                    self.domain,
                    latest_decided_ballot,
                    self.limit,
                )
            ):
                metric.add_metric(
                    [
                        self.domain,
                        self.domain_info,
                        label_value(index),
                        entry["ballot"],
                        entry["found"],
                        entry["resolution_type"],
                        entry["roll_hash"],
                        entry["data_hash"],
                        entry["data"],
                        entry["data_text"],
                    ],
                    1.0,
                )
            yield metric
        except Exception as e:
            print(f"Metric {self.label} failed: {e}", flush=True)


def is_bond_closed(bond):
    closed = bond.get("closed", False)
    if isinstance(closed, bool):
        return closed
    return str(closed).lower() in {"1", "true", "yes"}


def first_attestation(bond):
    attestations = bond.get("attestations") or {}
    if isinstance(attestations, list):
        return attestations[0] if attestations else {}
    if isinstance(attestations, dict):
        return attestations
    return {}


class CharBondsInfoCollector:
    def __init__(self, rpc, label, mode):
        self.rpc = rpc
        self.label = label
        self.mode = mode or "active"

    def collect(self):
        try:
            metric = GaugeMetricFamily(
                self.label,
                f"CHAR_BONDS_INFO:{self.mode}",
                labels=[
                    "txid",
                    "issuer",
                    "amount",
                    "closed",
                    "attestation_ballot",
                    "attestation_chain_id",
                    "genesis_char_hash",
                ],
            )
            for bond in self.rpc.getallcharbonds(1):
                if not isinstance(bond, dict):
                    continue

                closed = is_bond_closed(bond)
                if self.mode == "active" and closed:
                    continue

                attestation = first_attestation(bond)
                metric.add_metric(
                    [
                        label_value(bond.get("txid")),
                        label_value(bond.get("issuer")),
                        label_value(bond.get("amount")),
                        bool_label(closed),
                        label_value(attestation.get("ballot_number")),
                        label_value(attestation.get("chain_id")),
                        label_value(attestation.get("genesis_char_hash")),
                    ],
                    1.0,
                )
            yield metric
        except Exception as e:
            print(f"Metric {self.label} failed: {e}", flush=True)


def register_metric(rpc, labeled_cmd):
    if "=" not in labeled_cmd:
        return

    label, cmd = labeled_cmd.strip().split("=", 1)
    if not label or not cmd:
        return

    if cmd.startswith("CHAR_DOMAIN_INFO:"):
        args = cmd.removeprefix("CHAR_DOMAIN_INFO:")
        domain, domain_info = args.split(",", 1)
        REGISTRY.register(CharDomainInfoCollector(rpc, label, domain, domain_info))
        print(f"Metric created: {labeled_cmd}")
        return

    if cmd.startswith("CHAR_DOMAIN_DECISION_HISTORY:"):
        args = cmd.removeprefix("CHAR_DOMAIN_DECISION_HISTORY:")
        parts = args.split(",", 2)
        if len(parts) == 2:
            domain, domain_info = parts
            limit = DEFAULT_DECISION_HISTORY_LIMIT
        else:
            domain, domain_info, limit = parts
        REGISTRY.register(
            CharDomainDecisionHistoryCollector(rpc, label, domain, domain_info, limit)
        )
        print(f"Metric created: {labeled_cmd}")
        return

    if cmd.startswith("CHAR_BONDS_INFO:"):
        mode = cmd.removeprefix("CHAR_BONDS_INFO:")
        REGISTRY.register(CharBondsInfoCollector(rpc, label, mode))
        print(f"Metric created: {labeled_cmd}")
        return

    metric = Gauge(label, cmd)
    if cmd.startswith("COUNT:"):
        args = cmd.removeprefix("COUNT:")
        cmd, key, value = args.split(",", 2)
        func = make_counting_function(rpc, cmd, key, value)
    elif cmd.startswith("CHAR_DOMAIN_HASH:"):
        args = cmd.removeprefix("CHAR_DOMAIN_HASH:")
        domain, domain_info, key = args.split(",", 2)
        func = make_char_domain_function(rpc, domain, domain_info, key, fingerprint=True)
    elif cmd.startswith("CHAR_DOMAIN:"):
        args = cmd.removeprefix("CHAR_DOMAIN:")
        domain, domain_info, key = args.split(",", 2)
        func = make_char_domain_function(rpc, domain, domain_info, key)
    elif cmd.startswith("HASH:"):
        cmd = cmd.removeprefix("HASH:")
        func = make_hash_function(rpc, cmd)
    else:
        func = make_metric_function(rpc, cmd)

    metric.set_function(safe_metric(label, func))
    print(f"Metric created: {labeled_cmd}")


def register_metrics(rpc, metrics):
    for labeled_cmd in metrics.split():
        register_metric(rpc, labeled_cmd)


def main():
    # Port where prometheus server will scrape metrics data
    metrics_port = int(os.environ.get("METRICS_PORT", "9332"))

    # Bitcoin Core RPC data to scrape. Expressed as labeled RPC queries separated by spaces
    # label=method(params)[return object key][...]
    metrics = os.environ.get("METRICS", DEFAULT_METRICS)

    # Set up bitcoind RPC client
    rpc = AuthServiceProxy(
        service_url=f"http://{BITCOIN_RPC_USER}:{BITCOIN_RPC_PASSWORD}@{BITCOIN_RPC_HOST}:{BITCOIN_RPC_PORT}"
    )

    register_metrics(rpc, metrics)

    # Start the server
    server, thread = start_http_server(metrics_port)

    print(f"Server: {server}")
    print(f"Thread: {thread}")

    # Keep alive by waiting for endless loop to end
    thread.join()
    server.shutdown()


if __name__ == "__main__":
    main()
