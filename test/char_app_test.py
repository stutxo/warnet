#!/usr/bin/env python3

import importlib.util
import logging
import sys
import unittest
from pathlib import Path


CHAR_APP_PATH = (
    Path(__file__).resolve().parents[1]
    / "resources"
    / "images"
    / "char-app"
    / "char_app.py"
)

spec = importlib.util.spec_from_file_location("char_app", CHAR_APP_PATH)
char_app = importlib.util.module_from_spec(spec)
sys.modules["char_app"] = char_app
spec.loader.exec_module(char_app)


def decision_entry(domain_payload_counter: int, ballot: int) -> dict:
    payload = char_app.encode_counter_payload(domain_payload_counter).hex()
    return {
        "found": True,
        "ballot_number": ballot,
        "resolution_type": "decision",
        "decision_roll": {
            "data": payload,
            "serialized": f"abcd{ballot}",
            "roll_hash": f"roll{ballot}",
            "data_hash": f"data{ballot}",
        },
    }


def unresolved_entry(ballot: int) -> dict:
    return {
        "found": False,
        "ballot_number": ballot,
        "resolution_type": "unresolved",
    }


class CharAppHelpersTest(unittest.TestCase):
    def test_compact_size_roundtrip(self):
        values = [0, 1, 252, 253, 65535, 65536, 2**32, 2**64 - 1]
        for value in values:
            encoded = char_app.compact_size_encode(value)
            decoded, offset = char_app.compact_size_decode(encoded)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(encoded))

    def test_domain_notification_hash_matches_vector_encoding(self):
        preimage = "00" * 32
        expected = char_app.hashlib.sha256(b"\x20" + bytes.fromhex(preimage)).digest()
        self.assertEqual(char_app.domain_notification_hash(preimage), expected)

    def test_leader_and_decisionroll_parsing(self):
        domain_hash = b"\xaa" * 32
        leader_body = char_app.compact_size_encode(42) + domain_hash
        self.assertEqual(char_app.parse_leader_body(leader_body), (42, domain_hash))

        serialized = b"roll-bytes"
        decision_body = domain_hash + char_app.compact_size_encode(1) + serialized
        self.assertEqual(
            char_app.parse_decisionroll_body(decision_body),
            (domain_hash, 1, serialized),
        )

    def test_counter_payload(self):
        payload = char_app.encode_counter_payload(7)
        self.assertEqual(payload, b"warnet-counter:7")
        text, counter = char_app.decode_counter_payload(payload.hex())
        self.assertEqual(text, "warnet-counter:7")
        self.assertEqual(counter, 7)

        text, counter = char_app.decode_counter_payload("not-hex")
        self.assertIsNone(text)
        self.assertIsNone(counter)

        text, counter = char_app.decode_counter_payload(b"hello".hex())
        self.assertEqual(text, "hello")
        self.assertIsNone(counter)

    def test_previous_count_only_bootstraps_ballot_zero(self):
        self.assertEqual(char_app.previous_count_for_ballot(None, 0, 0), 0)
        self.assertIsNone(char_app.previous_count_for_ballot(None, 0, 5))
        self.assertEqual(char_app.previous_count_for_ballot(4, 9, 5), 9)
        self.assertIsNone(char_app.previous_count_for_ballot(4, 9, 6))

    def test_state_commits_and_clears_attempts(self):
        store = char_app.AppState()
        self.assertEqual(store.read_state(), (None, 0))
        store.mark_attempted(0)
        self.assertTrue(store.is_attempted(0))
        store.mark_accepted(
            0,
            1,
            char_app.encode_counter_payload(1).hex(),
            "warnet-counter:1",
            "test",
            "init",
        )
        self.assertEqual(store.accepted_vote_for_ballot(0)["counter"], 1)

        resolution = char_app.Resolution(
            ballot=0,
            resolution_type="decision",
            data_hex=char_app.encode_counter_payload(1).hex(),
            data_text="warnet-counter:1",
            counter=1,
            serialized_hex="abcd",
            roll_hash="roll",
            data_hash="data",
        )
        store.commit_resolution(resolution, 1)
        self.assertEqual(store.read_state(), (0, 1))
        self.assertFalse(store.is_attempted(0))
        self.assertIsNone(store.accepted_vote_for_ballot(0))
        self.assertEqual(store.previous_count_for_ballot(1), 1)

    def test_extract_resolution_handles_invalid_counter_payload(self):
        entry = {
            "found": True,
            "ballot_number": 3,
            "resolution_type": "decision",
            "decision_roll": {
                "data": b"warnet-counter:not-a-number".hex(),
                "serialized": "abcd",
                "roll_hash": "roll",
                "data_hash": "data",
            },
        }
        resolution = char_app.extract_resolution(entry, 3)
        self.assertEqual(resolution.ballot, 3)
        self.assertEqual(resolution.data_text, "warnet-counter:not-a-number")
        self.assertIsNone(resolution.counter)

    def test_extract_resolution_handles_impossible_and_unresolved_rolls(self):
        impossible = char_app.extract_resolution(
            {
                "found": True,
                "ballot_number": 4,
                "resolution_type": "impossible",
                "impossible_roll": {
                    "serialized": "abcd",
                    "roll_hash": "roll",
                    "data_hash": "data",
                },
            },
            4,
        )
        self.assertEqual(impossible.resolution_type, "impossible")
        self.assertEqual(impossible.roll_hash, "roll")
        self.assertEqual(impossible.data_hex, "")
        self.assertIsNone(impossible.counter)

        unresolved = char_app.extract_resolution(unresolved_entry(5), 5)
        self.assertEqual(unresolved.resolution_type, "unresolved")
        self.assertEqual(unresolved.ballot, 5)
        self.assertEqual(unresolved.data_text, "")

    def test_bootstrap_ballot_zero_uses_init_mode(self):
        domain = "aa" * 32

        class FakeRPC:
            def __init__(self):
                self.calls = []

            def call(self, method, *params):
                self.calls.append((method, params))
                if method == "addreferendumvote":
                    return {domain: True}
                raise AssertionError(f"unexpected RPC method {method}")

        app = char_app.CharApp.__new__(char_app.CharApp)
        app.domain_preimage = domain
        app.store = char_app.AppState()
        app.rpc = FakeRPC()
        app.pod_name = "tank-test"
        app.domain_info = "warnet"
        app.log = logging.getLogger("char-app-test")

        self.assertTrue(app.bootstrap_ballot_zero())
        self.assertEqual(
            app.rpc.calls,
            [
                (
                    "addreferendumvote",
                    (
                        [{domain: char_app.encode_counter_payload(1).hex()}],
                        "init",
                    ),
                )
            ],
        )
        self.assertEqual(app.store.accepted_vote_for_ballot(0)["mode"], "init")

    def test_leader_zmq_submission_uses_is_leader_after_prior_state(self):
        domain = "aa" * 32

        class FakeRPC:
            def __init__(self):
                self.calls = []

            def call(self, method, *params):
                self.calls.append((method, params))
                if method == "addreferendumvote":
                    return {domain: True}
                raise AssertionError(f"unexpected RPC method {method}")

        app = char_app.CharApp.__new__(char_app.CharApp)
        app.domain_info = "warnet"
        app.domain_preimage = domain
        app.domain_hash = char_app.domain_notification_hash(domain)
        app.store = char_app.AppState(last_ballot=4, counter=4)
        app.rpc = FakeRPC()
        app.pod_name = "tank-test"
        app.log = logging.getLogger("char-app-test")

        body = char_app.compact_size_encode(5) + app.domain_hash
        app.handle_leader_notification(body)

        self.assertEqual(
            app.rpc.calls,
            [
                (
                    "addreferendumvote",
                    (
                        [{domain: char_app.encode_counter_payload(5).hex()}],
                        "is_leader",
                    ),
                )
            ],
        )
        self.assertEqual(app.store.accepted_vote_for_ballot(5)["reason"], "leader-zmq")

    def test_leader_zmq_catches_up_previous_roll_before_submit(self):
        domain = "aa" * 32

        class FakeRPC:
            def __init__(self):
                self.calls = []

            def call(self, method, *params):
                self.calls.append((method, params))
                if method == "getreferendumresolution":
                    ballot = params[1]
                    if ballot == 0:
                        return [decision_entry(1, 0)]
                    return [unresolved_entry(ballot)]
                if method == "addreferendumvote":
                    return {domain: True}
                raise AssertionError(f"unexpected RPC method {method}")

        app = char_app.CharApp.__new__(char_app.CharApp)
        app.domain_info = "warnet"
        app.domain_preimage = domain
        app.domain_hash = char_app.domain_notification_hash(domain)
        app.store = char_app.AppState()
        app.rpc = FakeRPC()
        app.pod_name = "tank-test"
        app.log = logging.getLogger("char-app-test")
        app.max_process_rounds = 4

        body = char_app.compact_size_encode(1) + app.domain_hash
        app.handle_leader_notification(body)

        self.assertEqual(app.store.read_state(), (0, 1))
        add_calls = [call for call in app.rpc.calls if call[0] == "addreferendumvote"]
        self.assertEqual(
            add_calls[0][1],
            ([{domain: char_app.encode_counter_payload(2).hex()}], "is_leader"),
        )

    def test_resolution_processing_stops_at_unresolved_ballot_zero(self):
        domain = "aa" * 32

        class FakeRPC:
            def call(self, method, *params):
                if method != "getreferendumresolution":
                    raise AssertionError(f"unexpected RPC method {method}")
                return [unresolved_entry(params[1])]

        app = char_app.CharApp.__new__(char_app.CharApp)
        app.domain_preimage = domain
        app.store = char_app.AppState()
        app.rpc = FakeRPC()
        app.pod_name = "tank-test"
        app.log = logging.getLogger("char-app-test")
        app.max_process_rounds = 4

        self.assertEqual(app.process_available_decision_rolls(), 0)
        self.assertEqual(app.store.read_state(), (None, 0))

    def test_resolution_processing_commits_in_order(self):
        domain = "aa" * 32

        class FakeRPC:
            def call(self, method, *params):
                if method != "getreferendumresolution":
                    raise AssertionError(f"unexpected RPC method {method}")
                ballot = params[1]
                if ballot == 0:
                    return [decision_entry(1, 0)]
                if ballot == 1:
                    return [
                        {
                            "found": True,
                            "ballot_number": 1,
                            "resolution_type": "impossible",
                            "impossible_roll": {
                                "serialized": "beef",
                                "roll_hash": "roll1",
                                "data_hash": "data1",
                            },
                        }
                    ]
                if ballot == 2:
                    return [decision_entry(2, 2)]
                return [unresolved_entry(ballot)]

        app = char_app.CharApp.__new__(char_app.CharApp)
        app.domain_preimage = domain
        app.domain_info = "warnet"
        app.store = char_app.AppState()
        app.rpc = FakeRPC()
        app.pod_name = "tank-test"
        app.log = logging.getLogger("char-app-test")
        app.max_process_rounds = 8

        self.assertEqual(app.process_available_decision_rolls(), 3)
        self.assertEqual(app.store.read_state(), (2, 2))


if __name__ == "__main__":
    unittest.main()
