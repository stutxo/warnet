#!/usr/bin/env python3

from decimal import Decimal

try:
    from commander import Commander
except Exception:
    from resources.scenarios.commander import Commander

try:
    from char_framework.char_util import CharUtil, str_to_hex
except Exception:
    from resources.scenarios.char_framework.char_util import CharUtil, str_to_hex

DEFAULT_DOMAIN_INFO = "warnet"
DEFAULT_DOMAIN_PREIMAGE = str_to_hex(DEFAULT_DOMAIN_INFO)
DEFAULT_DUMMY_APP_PAYLOAD = "hello_char"
COINBASE_MATURITY = 100
CHAR_STAKE_EPOCH_LENGTH_BLOCKS = 18
CHAR_STAKE_ACTIVATION_LAG_EPOCHS = 2


def decimal_arg(value):
    amount = Decimal(value)
    if amount <= 0:
        raise ValueError("amount must be positive")
    return amount


class CharSetup(Commander):
    def set_test_params(self):
        self.num_nodes = 0

    def add_options(self, parser):
        parser.description = "Create one Char bond per tank and schedule an app domain"
        parser.usage = "warnet run /path/to/char_setup.py [options]"
        parser.add_argument(
            "--domain-preimage",
            default=DEFAULT_DOMAIN_PREIMAGE,
            help=f"Domain preimage hex to schedule (default: {DEFAULT_DOMAIN_PREIMAGE})",
        )
        parser.add_argument(
            "--domain-info",
            default=DEFAULT_DOMAIN_INFO,
            help=f"Domain info/name to store in the registry (default: {DEFAULT_DOMAIN_INFO})",
        )
        parser.add_argument(
            "--stake-amount",
            type=decimal_arg,
            default=Decimal("0.5"),
            help="Stake amount for each bond in BTC (default: 0.5)",
        )
        parser.add_argument(
            "--funding-amount",
            type=decimal_arg,
            default=None,
            help="BTC to send from tank 0 to each other tank before bond creation",
        )
        parser.add_argument(
            "--activation-blocks",
            type=int,
            default=None,
            help="Override the number of blocks to mine after bond confirmation",
        )
        parser.add_argument(
            "--force-new-bonds",
            action="store_true",
            help="Create a new bond even when a tank already owns one",
        )
        parser.add_argument(
            "--skip-activation",
            action="store_true",
            help="Skip mining to the active Char stake snapshot",
        )
        parser.add_argument(
            "--dummy-app-payload",
            default=DEFAULT_DUMMY_APP_PAYLOAD,
            help=f"String payload to submit to the scheduled domain (default: {DEFAULT_DUMMY_APP_PAYLOAD})",
        )
        parser.add_argument(
            "--skip-dummy-app",
            action="store_true",
            help="Skip submitting the dummy app payload after scheduling the domain",
        )

    def wait_char_indexes_caught_up(self, timeout=120):
        def ready():
            heights = [node.getblockcount() for node in self.nodes]
            if len(set(heights)) != 1:
                return False
            height = heights[0]

            for node in self.nodes:
                index_info = node.getindexinfo().get("charbondindex")
                if index_info is None:
                    raise AssertionError(
                        f"{node.tank} is missing charbondindex; set charenable=1"
                    )
                if not index_info.get("synced", False):
                    return False
                if index_info.get("best_block_height") != height:
                    return False
            return True

        self.wait_until(ready, timeout=timeout)

    def mine_to_wallet(self, node, wallet, blocks):
        if blocks <= 0:
            return []
        address = wallet.getnewaddress()
        return self.generatetoaddress(node, blocks, address)

    def generate_and_wait(self, node, blocks, *, wait_for_indexes=True, **kwargs):
        hashes = self.generate(node, blocks, **kwargs)
        if wait_for_indexes and blocks > 0:
            self.wait_char_indexes_caught_up()
        return hashes

    def activation_blocks_from_tip(self):
        current_height = self.nodes[0].getblockcount()
        epoch_length = CHAR_STAKE_EPOCH_LENGTH_BLOCKS
        next_snapshot = (
            (current_height + epoch_length - 1) // epoch_length
        ) * epoch_length
        target_height = next_snapshot + (
            CHAR_STAKE_ACTIVATION_LAG_EPOCHS * epoch_length
        )
        return max(0, target_height - current_height)

    def owned_bonds(self, node):
        return node.getallcharbonds(0)

    def ensure_source_funds(self, source_node, source_wallet, funding_amount):
        peer_funding = funding_amount * max(0, len(self.nodes) - 1)
        source_reserve = self.options.stake_amount + Decimal("3")
        required = peer_funding + source_reserve
        balance = source_wallet.getbalance("*", 1, False)

        if balance >= required:
            self.log.info(
                f"{source_node.tank} wallet has {balance} BTC; required {required} BTC"
            )
            return

        self.log.info(
            f"{source_node.tank} wallet has {balance} BTC; mining "
            f"{COINBASE_MATURITY + 1} blocks for setup funds"
        )
        self.mine_to_wallet(source_node, source_wallet, COINBASE_MATURITY + 1)
        self.wait_char_indexes_caught_up()

    def fund_peer_wallets(self, source_node, source_wallet, wallets, funding_amount):
        txids = []
        for node in self.nodes[1:]:
            wallet = wallets[node.tank]
            balance = wallet.getbalance("*", 1, False)
            if balance >= funding_amount:
                self.log.info(f"{node.tank} wallet already has {balance} BTC")
                continue

            address = wallet.getnewaddress()
            txid = source_wallet.sendtoaddress(address, funding_amount)
            txids.append(txid)
            self.log.info(f"funding {node.tank} with {funding_amount} BTC: {txid}")

        if not txids:
            return

        self.mine_to_wallet(source_node, source_wallet, 1)
        self.wait_char_indexes_caught_up()

    def create_bonds(self, wallets):
        created = []
        for node in self.nodes:
            existing = self.owned_bonds(node)
            if existing and not self.options.force_new_bonds:
                self.log.info(
                    f"{node.tank} already owns {len(existing)} bond(s); skipping"
                )
                continue

            tx = CharUtil(node, self).create_bond(
                self.options.stake_amount,
                return_tx=True,
                stake_amount=self.options.stake_amount,
                generate_block=False,
                wait_for_indexes=False,
            )
            created.append(tx["txid"])
            self.log.info(f"{node.tank} created bond {tx['txid']}")

        if not created:
            return created

        self.sync_mempools(timeout=120)
        self.mine_to_wallet(self.nodes[0], wallets[self.nodes[0].tank], 1)
        self.wait_char_indexes_caught_up(timeout=180)
        return created

    def activate_bonds(self, wallets):
        if self.options.skip_activation:
            self.log.info("Skipping stake activation mining")
            return

        blocks = self.options.activation_blocks
        if blocks is None:
            blocks = self.activation_blocks_from_tip()
        self.log.info(f"Mining {blocks} block(s) for active Char stake snapshot")
        self.mine_to_wallet(self.nodes[0], wallets[self.nodes[0].tank], blocks)
        self.wait_char_indexes_caught_up(timeout=180)

    def schedule_domain(self):
        for node in self.nodes:
            result = node.domain_registry(
                "schedule",
                self.options.domain_preimage,
                self.options.domain_info,
            )
            if not result.get("success"):
                raise AssertionError(f"{node.tank} failed to schedule domain")
            self.log.info(
                f"{node.tank} scheduled domain {self.options.domain_info} "
                f"({self.options.domain_preimage})"
            )

        for node in self.nodes:
            scheduled = node.domain_registry("list")
            if not any(
                entry.get("info") == self.options.domain_info for entry in scheduled
            ):
                raise AssertionError(
                    f"{node.tank} registry does not list {self.options.domain_info}"
                )

    def submit_dummy_app_payload(self):
        if self.options.skip_dummy_app:
            self.log.info("Skipping dummy app payload")
            return None

        payload = self.options.dummy_app_payload
        if not payload:
            raise AssertionError("dummy app payload cannot be empty")

        domain = self.options.domain_preimage
        payload_hex = payload.encode("utf-8").hex()
        leader_infos = []

        for node in self.nodes:
            info = node.getdomaininfo(domain)
            leader_infos.append((node.tank, info["next_ballot"], info["next_leader_bond"]))
            if not info["is_next_leader_mine"]:
                continue

            result = node.addreferendumvote([{domain: payload_hex}], "is_leader")
            if not result.get(domain):
                raise AssertionError(f"{node.tank} failed to submit dummy app payload")

            self.log.info(
                f"{node.tank} submitted dummy app payload {payload!r} "
                f"to {self.options.domain_info} at ballot {info['next_ballot']}"
            )
            return {
                "leader": node,
                "ballot": info["next_ballot"],
                "payload_hex": payload_hex,
            }

        raise AssertionError(f"No local tank owns the next leader bond: {leader_infos}")

    def wait_domain_decision_agreement(self, ballot, payload_hex, timeout=180):
        domain = self.options.domain_preimage
        last_snapshots = []

        def snapshot():
            nonlocal last_snapshots
            snapshots = []
            decisions = set()

            for node in self.nodes:
                try:
                    result = node.getreferendumresolution(domain, ballot, ballot, 2)
                except Exception as e:
                    snapshots.append({"tank": node.tank, "error": str(e)})
                    last_snapshots = snapshots
                    return False

                if not result:
                    snapshots.append({"tank": node.tank, "found": False})
                    last_snapshots = snapshots
                    return False

                entry = result[0]
                roll = entry.get("decision_roll") or {}
                roll_hash = roll.get("roll_hash")
                data_hash = roll.get("data_hash")
                data = roll.get("data")
                node_snapshot = {
                    "tank": node.tank,
                    "found": entry.get("found"),
                    "resolution_type": entry.get("resolution_type"),
                    "roll_hash": roll_hash,
                    "data_hash": data_hash,
                    "data": data,
                }
                snapshots.append(node_snapshot)

                if not entry.get("found") or entry.get("resolution_type") != "decision":
                    last_snapshots = snapshots
                    return False
                if not roll_hash or not data_hash or data != payload_hex:
                    last_snapshots = snapshots
                    return False

                decisions.add((roll_hash, data_hash))

            last_snapshots = snapshots
            return len(decisions) == 1

        try:
            self.wait_until(snapshot, timeout=timeout)
        except AssertionError as e:
            raise AssertionError(
                f"Timed out waiting for {self.options.domain_info} ballot {ballot} "
                f"decision agreement: {last_snapshots}"
            ) from e

        agreed = last_snapshots[0]
        self.log.info(
            f"Verified {self.options.domain_info} ballot {ballot} decision agreement: "
            f"roll_hash={agreed['roll_hash']} data_hash={agreed['data_hash']}"
        )

    def verify_bonds(self):
        missing_owned = []
        for node in self.nodes:
            if not self.owned_bonds(node):
                missing_owned.append(node.tank)
        if missing_owned:
            raise AssertionError(f"nodes missing owned Char bonds: {missing_owned}")

        seen = self.nodes[0].getallcharbonds()
        if len(seen) < len(self.nodes):
            raise AssertionError(
                f"expected at least {len(self.nodes)} visible bonds, saw {len(seen)}"
            )
        self.log.info(f"Verified {len(seen)} visible Char bond(s)")

    def verify_domain_roll_agreement(self):
        rolls = []
        for node in self.nodes:
            info = node.getdomaininfo(self.options.domain_preimage)
            rolls.append(
                {
                    "tank": node.tank,
                    "next_ballot": info["next_ballot"],
                    "next_leader_bond": info["next_leader_bond"],
                    "is_next_leader_mine": info["is_next_leader_mine"],
                }
            )

        expected = (rolls[0]["next_ballot"], rolls[0]["next_leader_bond"])
        mismatches = [
            roll
            for roll in rolls
            if (roll["next_ballot"], roll["next_leader_bond"]) != expected
        ]
        if mismatches:
            raise AssertionError(
                f"Char domain roll disagreement for {self.options.domain_info}: {rolls}"
            )

        leaders = [roll["tank"] for roll in rolls if roll["is_next_leader_mine"]]
        self.log.info(
            f"Verified Char roll agreement for {self.options.domain_info}: "
            f"next_ballot={expected[0]} next_leader_bond={expected[1]} "
            f"local_leaders={leaders}"
        )

    def run_test(self):
        if not self.nodes:
            raise AssertionError("No tanks found")

        self.wait_for_tanks_connected()
        wallets = {node.tank: self.ensure_miner(node) for node in self.nodes}
        funding_amount = self.options.funding_amount
        if funding_amount is None:
            funding_amount = self.options.stake_amount + Decimal("3")

        source_node = self.nodes[0]
        source_wallet = wallets[source_node.tank]
        self.ensure_source_funds(source_node, source_wallet, funding_amount)
        self.fund_peer_wallets(source_node, source_wallet, wallets, funding_amount)
        self.create_bonds(wallets)
        self.activate_bonds(wallets)
        self.verify_bonds()
        self.schedule_domain()
        self.verify_domain_roll_agreement()
        dummy_vote = self.submit_dummy_app_payload()
        if dummy_vote is not None:
            CharUtil(dummy_vote["leader"], self).attest_bonds_and_wait()
            for node in self.nodes:
                CharUtil(node, self).attest_bonds_and_wait(wait_for_global_indexes=False)
            self.wait_char_indexes_caught_up(timeout=180)
            self.wait_domain_decision_agreement(
                dummy_vote["ballot"],
                dummy_vote["payload_hex"],
            )
        self.verify_domain_roll_agreement()

        for node in self.nodes:
            info = node.getdomaininfo(self.options.domain_preimage)
            self.log.info(
                f"{node.tank} domain next_ballot={info['next_ballot']} "
                f"is_next_leader_mine={info['is_next_leader_mine']}"
            )


def main():
    CharSetup("").main()


if __name__ == "__main__":
    main()
