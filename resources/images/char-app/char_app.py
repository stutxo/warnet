#!/usr/bin/env python3

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


DEFAULT_DOMAIN_INFO = "warnet"
DEFAULT_DOMAIN_PREIMAGE = (
    "edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090"
)
COUNTER_PREFIX = "warnet-counter:"
DECISION_ROLL_TAG = 1
IMPOSSIBLE_ROLL_TAG = 2
RPC_TIMEOUT_SECONDS = 15
RPC_RETRY_SECONDS = 5
RESOLUTION_VERBOSITY = 1
DEFAULT_POLL_INTERVAL_SECONDS = 1
DEFAULT_MAX_PROCESS_ROUNDS = 100
LEADER_PREVIOUS_BALLOT_WAIT_SECONDS = 5
LEADER_PREVIOUS_BALLOT_POLL_SECONDS = 0.25


def setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def compact_size_encode(value: int) -> bytes:
    if value < 0:
        raise ValueError("CompactSize value cannot be negative")
    if value < 253:
        return value.to_bytes(1, "little")
    if value <= 0xFFFF:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    if value <= 0xFFFFFFFFFFFFFFFF:
        return b"\xff" + value.to_bytes(8, "little")
    raise ValueError("CompactSize value too large")


def compact_size_decode(data: bytes, offset: int = 0) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("missing CompactSize prefix")

    first = data[offset]
    offset += 1
    if first < 253:
        return first, offset

    size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
    if offset + size > len(data):
        raise ValueError("truncated CompactSize payload")
    return int.from_bytes(data[offset : offset + size], "little"), offset + size


def normalize_hex(value: str) -> str:
    value = value.strip()
    if value.startswith("0x"):
        value = value[2:]
    return value


def domain_notification_hash(domain_preimage_hex: str) -> bytes:
    domain_bytes = bytes.fromhex(normalize_hex(domain_preimage_hex))
    return hashlib.sha256(compact_size_encode(len(domain_bytes)) + domain_bytes).digest()


def parse_leader_body(body: bytes) -> tuple[int, bytes]:
    ballot, offset = compact_size_decode(body)
    if offset + 32 > len(body):
        raise ValueError("leader notification missing domain hash")
    return ballot, body[offset : offset + 32]


def parse_decisionroll_body(body: bytes) -> tuple[bytes, int, bytes]:
    if len(body) < 32:
        raise ValueError("decisionroll notification missing domain hash")
    domain_hash = body[:32]
    tag, offset = compact_size_decode(body, 32)
    return domain_hash, tag, body[offset:]


def decode_counter_payload(data_hex: str | None) -> tuple[str | None, int | None]:
    if not data_hex:
        return "", None
    try:
        text = bytes.fromhex(normalize_hex(data_hex)).decode("utf-8")
    except Exception:
        return None, None
    if not text.startswith(COUNTER_PREFIX):
        return text, None
    try:
        return text, int(text.removeprefix(COUNTER_PREFIX))
    except ValueError:
        return text, None


def encode_counter_payload(counter: int) -> bytes:
    if counter < 0:
        raise ValueError("counter cannot be negative")
    return f"{COUNTER_PREFIX}{counter}".encode("utf-8")


def next_ballot_to_process(last_ballot: int | None) -> int:
    if last_ballot is None:
        return 0
    if last_ballot >= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("ballot overflow")
    return last_ballot + 1


def previous_count_for_ballot(
    last_ballot: int | None,
    counter: int,
    ballot: int,
) -> int | None:
    if ballot == 0 and last_ballot is None:
        return counter
    if last_ballot == ballot - 1:
        return counter
    return None


@dataclass(frozen=True)
class Resolution:
    ballot: int
    resolution_type: str
    data_hex: str
    data_text: str | None
    counter: int | None
    serialized_hex: str
    roll_hash: str
    data_hash: str


class RpcError(Exception):
    def __init__(self, message: str, rpc_error: dict[str, Any] | None = None):
        super().__init__(message)
        self.rpc_error = rpc_error or {}


class BitcoinRPC:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        timeout: int = RPC_TIMEOUT_SECONDS,
    ):
        self.url = f"http://{host}:{port}"
        self.timeout = timeout
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        self.request_id = 0

    def call(self, method: str, *params: Any) -> Any:
        self.request_id += 1
        payload = json.dumps(
            {
                "jsonrpc": "1.0",
                "id": self.request_id,
                "method": method,
                "params": list(params),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                decoded = json.loads(body.decode("utf-8"))
            except Exception as err:
                raise RpcError(f"{method} HTTP {exc.code}: {body!r}") from err
            rpc_error = decoded.get("error")
            if rpc_error:
                raise RpcError(
                    rpc_error.get("message", str(rpc_error)),
                    rpc_error=rpc_error,
                )
            raise RpcError(f"{method} HTTP {exc.code}: {decoded}") from exc
        except urllib.error.URLError as exc:
            raise RpcError(f"{method} connection failed: {exc}") from exc

        decoded = json.loads(body.decode("utf-8"))
        rpc_error = decoded.get("error")
        if rpc_error:
            raise RpcError(rpc_error.get("message", str(rpc_error)), rpc_error=rpc_error)
        return decoded.get("result")


@dataclass
class AppState:
    last_ballot: int | None = None
    counter: int = 0
    attempted_ballots: set[int] = field(default_factory=set)
    accepted_votes: dict[int, dict[str, Any]] = field(default_factory=dict)

    def read_state(self) -> tuple[int | None, int]:
        return self.last_ballot, self.counter

    def previous_count_for_ballot(self, ballot: int) -> int | None:
        return previous_count_for_ballot(self.last_ballot, self.counter, ballot)

    def is_attempted(self, ballot: int) -> bool:
        return ballot in self.attempted_ballots

    def mark_attempted(self, ballot: int) -> None:
        self.attempted_ballots.add(ballot)

    def mark_accepted(
        self,
        ballot: int,
        counter: int,
        payload_hex: str,
        payload_text: str,
        reason: str,
        mode: str,
    ) -> None:
        self.attempted_ballots.add(ballot)
        self.accepted_votes[ballot] = {
            "ballot": ballot,
            "counter": counter,
            "payload_hex": payload_hex,
            "payload_text": payload_text,
            "reason": reason,
            "mode": mode,
        }

    def accepted_vote_for_ballot(self, ballot: int) -> dict[str, Any] | None:
        return self.accepted_votes.get(ballot)

    def commit_resolution(self, resolution: Resolution, counter: int) -> None:
        self.last_ballot = resolution.ballot
        self.counter = counter
        self.attempted_ballots = {
            ballot for ballot in self.attempted_ballots if ballot > resolution.ballot
        }
        self.accepted_votes = {
            ballot: vote
            for ballot, vote in self.accepted_votes.items()
            if ballot > resolution.ballot
        }


def extract_resolution(entry: dict[str, Any], fallback_ballot: int) -> Resolution:
    ballot = int(entry.get("ballot_number", fallback_ballot))
    if not entry.get("found", False):
        return Resolution(
            ballot=ballot,
            resolution_type=entry.get("resolution_type") or "unresolved",
            data_hex="",
            data_text="",
            counter=None,
            serialized_hex="",
            roll_hash="",
            data_hash="",
        )

    resolution_type = entry.get("resolution_type", "")
    if resolution_type == "impossible":
        roll = entry.get("impossible_roll") or {}
        data_hex = ""
        data_text = ""
        counter = None
    else:
        roll = entry.get("decision_roll") or {}
        data_hex = normalize_hex(roll.get("data") or "")
        data_text, counter = decode_counter_payload(data_hex)

    return Resolution(
        ballot=ballot,
        resolution_type=resolution_type,
        data_hex=data_hex,
        data_text=data_text,
        counter=counter,
        serialized_hex=normalize_hex(roll.get("serialized") or ""),
        roll_hash=roll.get("roll_hash") or "",
        data_hash=roll.get("data_hash") or "",
    )


def is_found_resolution(resolution: Resolution) -> bool:
    return resolution.resolution_type not in ("", "unresolved")


def is_domaininfo_resolution_gap(exc: Exception) -> bool:
    return "Resolution for latest decided ballot not found" in str(exc)


class CharApp:
    def __init__(self):
        self.domain_info = os.environ.get("CHAR_DOMAIN_INFO", DEFAULT_DOMAIN_INFO)
        self.domain_preimage = os.environ.get(
            "CHAR_DOMAIN_PREIMAGE",
            DEFAULT_DOMAIN_PREIMAGE,
        )
        self.domain_hash = domain_notification_hash(self.domain_preimage)
        self.store = AppState()
        self.rpc = BitcoinRPC(
            os.environ.get("CHAR_RPC_HOST", "127.0.0.1"),
            int(os.environ.get("CHAR_RPC_PORT", "18443")),
            os.environ.get("CHAR_RPC_USER", "user"),
            os.environ.get("CHAR_RPC_PASSWORD", "gn0cchi"),
        )
        self.zmq_url = os.environ.get("CHAR_ZMQ_URL", "tcp://127.0.0.1:28332")
        self.poll_interval = float(
            os.environ.get(
                "CHAR_POLL_INTERVAL_SECONDS",
                str(DEFAULT_POLL_INTERVAL_SECONDS),
            )
        )
        self.max_process_rounds = int(
            os.environ.get(
                "CHAR_DECISION_SCAN_WINDOW",
                str(DEFAULT_MAX_PROCESS_ROUNDS),
            )
        )
        self.pod_name = os.environ.get("POD_NAME", "unknown")
        self.log = logging.getLogger("char-app")

    def structured_event(self, event: str, **fields: Any) -> dict[str, Any]:
        return {
            "event": event,
            "pod": self.pod_name,
            "domain_info": self.domain_info,
            "domain_preimage": self.domain_preimage,
            **fields,
        }

    def log_event(self, event: str, **fields: Any) -> None:
        self.log.info(
            "CHAR_APP_EVENT %s",
            json.dumps(self.structured_event(event, **fields), sort_keys=True),
        )

    def log_bug_evidence(
        self,
        reason: str,
        event: str = "accepted_vote_finalized_differently",
        **fields: Any,
    ) -> None:
        self.log.error(
            "CHAR_APP_BUG_EVIDENCE %s",
            json.dumps(
                self.structured_event(event, reason=reason, **fields),
                sort_keys=True,
            ),
        )

    def wait_for_rpc_and_bond(self) -> None:
        while True:
            try:
                bonds = self.rpc.call("getallcharbonds", 0)
                if bonds:
                    self.log.info(
                        "local bond ready: pod=%s bond=%s",
                        self.pod_name,
                        bonds[0].get("txid"),
                    )
                    return
                self.log.info("waiting for local CHAR bond")
            except Exception as exc:
                self.log.info("waiting for local RPC/CHAR bond: %s", exc)
            time.sleep(RPC_RETRY_SECONDS)

    def wait_for_domain(self) -> None:
        while True:
            try:
                self.rpc.call("getdomaininfo", self.domain_preimage)
                self.log.info(
                    "scheduled CHAR domain ready: info=%s preimage=%s",
                    self.domain_info,
                    self.domain_preimage,
                )
                return
            except Exception as exc:
                if is_domaininfo_resolution_gap(exc):
                    self.log.info(
                        "scheduled CHAR domain visible with local resolution gap: %s",
                        exc,
                    )
                    return
                self.log.info("waiting for scheduled CHAR domain: %s", exc)
                time.sleep(RPC_RETRY_SECONDS)

    def fetch_resolutions(self, first_ballot: int, last_ballot: int) -> list[Resolution]:
        if last_ballot < first_ballot:
            return []
        results = self.rpc.call(
            "getreferendumresolution",
            self.domain_preimage,
            first_ballot,
            last_ballot,
            RESOLUTION_VERBOSITY,
        )
        if isinstance(results, dict):
            results = results.get("resolutions", [])
        if not isinstance(results, list):
            return []

        resolutions = []
        for offset, entry in enumerate(results):
            if isinstance(entry, dict):
                resolutions.append(extract_resolution(entry, first_ballot + offset))
        return sorted(resolutions, key=lambda resolution: resolution.ballot)

    def fetch_resolution(self, ballot: int) -> Resolution | None:
        resolutions = self.fetch_resolutions(ballot, ballot)
        if not resolutions:
            return None
        return resolutions[0]

    def commit_resolution(self, resolution: Resolution) -> None:
        last_ballot, counter = self.store.read_state()
        expected_ballot = next_ballot_to_process(last_ballot)
        if resolution.ballot != expected_ballot:
            raise ValueError(
                f"out-of-order resolution: expected ballot {expected_ballot}, "
                f"got {resolution.ballot}"
            )

        accepted_vote = self.store.accepted_vote_for_ballot(resolution.ballot)
        if accepted_vote is not None:
            finalized_expected_payload = (
                resolution.resolution_type == "decision"
                and resolution.data_hex == accepted_vote["payload_hex"]
            )
            if not finalized_expected_payload:
                self.log_bug_evidence(
                    "local RPC accepted a vote but the finalized roll did not contain it",
                    expected_vote=accepted_vote,
                    actual_resolution=resolution.__dict__,
                    local_state={"last_ballot": last_ballot, "counter": counter},
                )
            else:
                self.log_event(
                    "accepted_vote_finalized",
                    ballot=resolution.ballot,
                    counter=accepted_vote["counter"],
                    payload_hex=accepted_vote["payload_hex"],
                    roll_hash=resolution.roll_hash,
                    data_hash=resolution.data_hash,
                    mode=accepted_vote["mode"],
                    reason=accepted_vote["reason"],
                )

        next_counter = counter
        if resolution.resolution_type == "decision" and resolution.counter is not None:
            expected_counter = counter + 1
            if resolution.counter == expected_counter:
                next_counter = resolution.counter
                self.log.info(
                    "committed counter decision: ballot=%s counter=%s data=%s",
                    resolution.ballot,
                    next_counter,
                    resolution.data_text,
                )
            else:
                self.log.warning(
                    "committed decision with unexpected counter: ballot=%s "
                    "counter=%s expected=%s data=%s",
                    resolution.ballot,
                    resolution.counter,
                    expected_counter,
                    resolution.data_text,
                )
        elif resolution.resolution_type == "decision":
            self.log.info(
                "committed decision without counter: ballot=%s data=%s",
                resolution.ballot,
                resolution.data_text,
            )
        elif resolution.resolution_type == "impossible":
            self.log.info("committed impossible roll: ballot=%s", resolution.ballot)
        else:
            self.log.info(
                "committed non-decision roll: ballot=%s type=%s",
                resolution.ballot,
                resolution.resolution_type,
            )

        self.store.commit_resolution(resolution, next_counter)

    def process_next_decision_roll(self) -> bool:
        ballot = next_ballot_to_process(self.store.last_ballot)
        resolution = self.fetch_resolution(ballot)
        if resolution is None or not is_found_resolution(resolution):
            return False
        self.commit_resolution(resolution)
        return True

    def process_available_decision_rolls(self) -> int:
        applied = 0
        for _ in range(max(0, self.max_process_rounds)):
            try:
                advanced = self.process_next_decision_roll()
            except Exception as exc:
                self.log.debug("decision roll processing stopped: %s", exc)
                break
            if not advanced:
                break
            applied += 1
        if applied == self.max_process_rounds and self.max_process_rounds > 0:
            self.log.warning("decision processing round limit reached")
        return applied

    def submit_vote(
        self,
        ballot: int,
        reason: str,
        mode: str = "is_leader",
    ) -> bool:
        if self.store.is_attempted(ballot):
            return False

        previous_count = self.store.previous_count_for_ballot(ballot)
        if previous_count is None:
            self.log.info(
                "vote skipped until previous ballot is committed: ballot=%s reason=%s",
                ballot,
                reason,
            )
            return False

        counter = previous_count + 1
        payload = encode_counter_payload(counter)
        payload_hex = payload.hex()
        payload_text = payload.decode("utf-8")
        try:
            result = self.rpc.call(
                "addreferendumvote",
                [{self.domain_preimage: payload_hex}],
                mode,
            )
        except Exception as exc:
            self.log.info(
                "vote rejected by RPC: ballot=%s mode=%s payload=%s error=%s",
                ballot,
                mode,
                payload_text,
                exc,
            )
            return False

        if not isinstance(result, dict):
            self.log.info(
                "vote returned malformed result: ballot=%s mode=%s payload=%s result=%s",
                ballot,
                mode,
                payload_text,
                result,
            )
            return False

        accepted = bool(result.get(self.domain_preimage, False))
        self.store.mark_attempted(ballot)
        if not accepted:
            self.log.info(
                "vote not accepted: ballot=%s mode=%s payload=%s result=%s",
                ballot,
                mode,
                payload_text,
                result,
            )
            return False

        self.store.mark_accepted(
            ballot,
            counter,
            payload_hex,
            payload_text,
            reason,
            mode,
        )
        self.log.info(
            "submitted vote: ballot=%s counter=%s mode=%s payload=%s reason=%s",
            ballot,
            counter,
            mode,
            payload_text,
            reason,
        )
        self.log_event(
            "accepted_vote",
            ballot=ballot,
            counter=counter,
            mode=mode,
            payload_hex=payload_hex,
            payload_text=payload_text,
            reason=reason,
        )
        return True

    def bootstrap_ballot_zero(self) -> bool:
        if self.store.last_ballot is not None:
            return False
        return self.submit_vote(0, "startup-init", "init")

    def wait_for_previous_count_for_ballot(self, ballot: int) -> int | None:
        deadline = time.monotonic() + LEADER_PREVIOUS_BALLOT_WAIT_SECONDS
        while True:
            previous_count = self.store.previous_count_for_ballot(ballot)
            if previous_count is not None:
                return previous_count

            self.process_available_decision_rolls()
            previous_count = self.store.previous_count_for_ballot(ballot)
            if previous_count is not None:
                return previous_count

            if time.monotonic() >= deadline:
                return None
            time.sleep(LEADER_PREVIOUS_BALLOT_POLL_SECONDS)

    def submit_leader_vote(self, ballot: int) -> bool:
        if self.store.is_attempted(ballot):
            return False
        if self.wait_for_previous_count_for_ballot(ballot) is None:
            self.log.info("leader ballot skipped: missing previous state ballot=%s", ballot)
            return False
        return self.submit_vote(ballot, "leader-zmq", "is_leader")

    def handle_leader_notification(self, body: bytes) -> None:
        ballot, message_domain_hash = parse_leader_body(body)
        if message_domain_hash != self.domain_hash:
            return

        self.log.info("leader hint received: ballot=%s", ballot)
        self.submit_leader_vote(ballot)

    def handle_decisionroll_notification(self, body: bytes) -> None:
        message_domain_hash, tag, _serialized = parse_decisionroll_body(body)
        if message_domain_hash != self.domain_hash:
            return
        if tag not in (DECISION_ROLL_TAG, IMPOSSIBLE_ROLL_TAG):
            return

        self.log.info("decisionroll hint received: tag=%s", tag)
        applied = self.process_available_decision_rolls()
        if applied:
            self.log.info("decisionroll catch-up applied=%s", applied)

    def run_periodic_work(self) -> None:
        applied = self.process_available_decision_rolls()
        if applied:
            self.log.info("periodic decision catch-up applied=%s", applied)
        self.bootstrap_ballot_zero()

    def run(self) -> None:
        import zmq

        self.log.info(
            "starting CHAR app: pod=%s domain_info=%s domain_preimage=%s zmq=%s",
            self.pod_name,
            self.domain_info,
            self.domain_preimage,
            self.zmq_url,
        )
        self.wait_for_rpc_and_bond()
        self.wait_for_domain()
        self.process_available_decision_rolls()
        self.bootstrap_ballot_zero()

        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt(zmq.RCVHWM, 0)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.SUBSCRIBE, b"leader")
        socket.setsockopt(zmq.SUBSCRIBE, b"decisionroll")
        socket.connect(self.zmq_url)
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        self.log.info("ZMQ listener connected")

        try:
            while True:
                events = dict(poller.poll(int(self.poll_interval * 1000)))
                if socket not in events:
                    self.run_periodic_work()
                    continue
                frames = socket.recv_multipart()
                if len(frames) < 2:
                    continue
                topic, body = frames[:2]
                try:
                    if topic == b"leader":
                        self.handle_leader_notification(body)
                    elif topic == b"decisionroll":
                        self.handle_decisionroll_notification(body)
                except Exception as exc:
                    self.log.warning("failed to process %r notification: %s", topic, exc)
        finally:
            socket.close()
            context.term()


def main() -> None:
    setup_logging()
    CharApp().run()


if __name__ == "__main__":
    main()
