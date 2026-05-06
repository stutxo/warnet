#!/usr/bin/env python3

from decimal import Decimal
from threading import Thread
from time import sleep

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
MINING_TANK = "tank-0000"
STAKE_AMOUNT = Decimal("0.5")
FUNDING_AMOUNT = STAKE_AMOUNT + Decimal("3")
MINE_INTERVAL_SECONDS = 30
RPC_TIMEOUT_SECONDS = 6000
COINBASE_MATURITY = 100
REGTEST_COINBASE_SUBSIDY = Decimal("50")
CHAR_STAKE_EPOCH_LENGTH_BLOCKS = 18
CHAR_STAKE_ACTIVATION_LAG_EPOCHS = 2


def positive_int_arg(value):
    amount = int(value)
    if amount <= 0:
        raise ValueError("amount must be positive")
    return amount


class CharSetup(Commander):
    def set_test_params(self):
        self.num_nodes = 0
        self.rpc_timeout = RPC_TIMEOUT_SECONDS

    def add_options(self, parser):
        parser.description = (
            "Create one active CHAR bond per CHAR tank, schedule the default "
            "app domain, then mine one block every 30 seconds."
        )
        parser.usage = "warnet run /path/to/char_setup.py"
        parser.add_argument(
            "--mine-interval",
            type=positive_int_arg,
            default=MINE_INTERVAL_SECONDS,
            help=f"Seconds between mined blocks after setup (default: {MINE_INTERVAL_SECONDS})",
        )

    def wait_for_tanks_connected(self):
        def tank_connected(tank):
            expected = int(getattr(tank, "init_peers", 0) or 0)
            while True:
                try:
                    peers = tank.getpeerinfo()
                    manual = sum(
                        1
                        for peer in peers
                        if peer.get("connection_type") == "manual"
                        or peer.get("addnode") is True
                    )
                    char = sum(
                        1
                        for peer in peers
                        if peer.get("char_capable") is True
                        or "CHAR" in peer.get("servicesnames", [])
                    )
                    total = len(peers)
                    self.log.info(
                        f"Tank {tank.tank} connected "
                        f"manual={manual}/{expected} char={char} total={total}"
                    )
                    if expected == 0 or manual >= expected or char >= expected:
                        return
                except Exception as e:
                    self.log.warning(f"Couldn't get peer info from {tank.tank}: {e}")
                sleep(5)

        threads = [Thread(target=tank_connected, args=(node,)) for node in self.nodes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.log.info("Network connected")

    def select_mining_node(self):
        if MINING_TANK not in self.tanks:
            raise AssertionError(f"Expected mining tank named {MINING_TANK!r}")
        self.log.info(f"Using {MINING_TANK} as mining/funding source")
        return self.tanks[MINING_TANK]

    def discover_char_nodes(self):
        char_nodes = []
        for node in self.nodes:
            try:
                if node.getindexinfo().get("charbondindex") is not None:
                    char_nodes.append(node)
            except Exception:
                continue
        if not char_nodes:
            raise AssertionError("No CHAR tanks found; enable charenable=1")
        self.log.info(
            "CHAR participant tanks: " + ", ".join(node.tank for node in char_nodes)
        )
        return char_nodes

    def char_participants(self):
        return self.char_nodes

    def wait_char_indexes_caught_up(self, timeout=180):
        def ready():
            tips = [node.getbestblockhash() for node in self.nodes]
            if len(set(tips)) != 1:
                return False
            height = self.mining_node.getblockcount()
            for node in self.char_participants():
                if node.getblockcount() != height:
                    return False
                index_info = node.getindexinfo().get("charbondindex")
                if index_info is None:
                    raise AssertionError(f"{node.tank} is missing charbondindex")
                if not index_info.get("synced", False):
                    return False
                if index_info.get("best_block_height") != height:
                    return False
            return True

        self.wait_until(ready, timeout=timeout)

    def mine_to_wallet(self, node, wallet, blocks, *, wait_for_indexes=True):
        if blocks <= 0:
            return []
        address = wallet.getnewaddress()
        blocks_mined = self.generatetoaddress(node, blocks, address)
        if wait_for_indexes:
            self.wait_char_indexes_caught_up()
        return blocks_mined

    def activation_blocks_from_tip(self):
        height = self.mining_node.getblockcount()
        epoch = CHAR_STAKE_EPOCH_LENGTH_BLOCKS
        next_snapshot = ((height + epoch - 1) // epoch) * epoch
        activation_height = next_snapshot + (
            CHAR_STAKE_ACTIVATION_LAG_EPOCHS * epoch
        )
        return max(0, activation_height - height)

    def ensure_source_funds(self, source_node, source_wallet):
        funding_targets = [
            node for node in self.char_participants() if node.tank != source_node.tank
        ]
        source_reserve = (
            FUNDING_AMOUNT
            if source_node.tank in {node.tank for node in self.char_participants()}
            else Decimal("1")
        )
        required = FUNDING_AMOUNT * len(funding_targets) + source_reserve
        balance = source_wallet.getbalance("*", 1, False)
        if balance >= required:
            self.log.info(
                f"{source_node.tank} wallet has {balance} BTC; required {required} BTC"
            )
            return

        mature_outputs_needed = int(
            (required / REGTEST_COINBASE_SUBSIDY).to_integral_value(
                rounding="ROUND_CEILING"
            )
        )
        blocks = COINBASE_MATURITY + mature_outputs_needed
        self.log.info(
            f"{source_node.tank} wallet has {balance} BTC; mining {blocks} "
            f"setup block(s); required {required} BTC"
        )
        self.mine_to_wallet(source_node, source_wallet, blocks)

    def fund_peer_wallets(self, source_node, source_wallet, wallets):
        txids = []
        for node in self.char_participants():
            if node.tank == source_node.tank:
                continue
            wallet = wallets[node.tank]
            balance = wallet.getbalance("*", 1, False)
            if balance >= FUNDING_AMOUNT:
                self.log.info(f"{node.tank} wallet already has {balance} BTC")
                continue
            txid = source_wallet.sendtoaddress(wallet.getnewaddress(), FUNDING_AMOUNT)
            txids.append(txid)
            self.log.info(f"funding {node.tank} with {FUNDING_AMOUNT} BTC: {txid}")

        if txids:
            self.mine_to_wallet(source_node, source_wallet, 1)

    def owned_bonds(self, node):
        return node.getallcharbonds(0)

    def create_bonds(self):
        created = []
        for node in self.char_participants():
            existing = self.owned_bonds(node)
            if existing:
                self.log.info(
                    f"{node.tank} already owns {len(existing)} bond(s); skipping"
                )
                continue
            tx = CharUtil(node, self).create_bond(
                STAKE_AMOUNT,
                return_tx=True,
                stake_amount=STAKE_AMOUNT,
                generate_block=False,
                wait_for_indexes=False,
            )
            created.append(tx["txid"])
            self.log.info(f"{node.tank} created bond {tx['txid']}")

        if created:
            self.sync_mempools(timeout=120)
            self.mine_to_wallet(
                self.mining_node,
                self.wallets[self.mining_node.tank],
                1,
                wait_for_indexes=False,
            )
            self.wait_char_indexes_caught_up(timeout=180)

    def activate_bonds(self):
        blocks = self.activation_blocks_from_tip()
        self.log.info(f"Mining {blocks} block(s) for active CHAR stake snapshot")
        self.mine_to_wallet(
            self.mining_node,
            self.wallets[self.mining_node.tank],
            blocks,
            wait_for_indexes=False,
        )
        self.wait_char_indexes_caught_up(timeout=180)

    def expected_owned_bond_txids(self):
        expected = set()
        missing = []
        for node in self.char_participants():
            bonds = self.owned_bonds(node)
            if not bonds:
                missing.append(node.tank)
                continue
            expected.update(bond["txid"] for bond in bonds if bond.get("txid"))
        if missing:
            raise AssertionError(f"nodes missing owned CHAR bonds: {missing}")
        return expected

    def wait_for_active_bonds(self):
        expected = self.expected_owned_bond_txids()
        if len(expected) < len(self.char_participants()):
            raise AssertionError(
                f"expected at least {len(self.char_participants())} bonds, "
                f"saw {len(expected)}"
            )

        def active_everywhere():
            for node in self.char_participants():
                active = {
                    bond["txid"]
                    for bond in node.getallcharbonds(1)
                    if bond.get("txid") and not bond.get("closed", False)
                }
                if not expected.issubset(active):
                    return False
            return True

        self.wait_until(active_everywhere, timeout=180)
        self.log.info(
            f"Verified {len(expected)} active CHAR bond(s) on "
            f"{len(self.char_participants())} tank(s)"
        )

    def schedule_domain(self):
        for node in self.char_participants():
            scheduled = node.domain_registry("list")
            if any(entry.get("info") == DEFAULT_DOMAIN_INFO for entry in scheduled):
                self.log.info(
                    f"{node.tank} already has scheduled domain {DEFAULT_DOMAIN_INFO}"
                )
                continue
            result = node.domain_registry(
                "schedule",
                DEFAULT_DOMAIN_PREIMAGE,
                DEFAULT_DOMAIN_INFO,
            )
            if not result.get("success"):
                raise AssertionError(f"{node.tank} failed to schedule domain")
            self.log.info(
                f"{node.tank} scheduled domain {DEFAULT_DOMAIN_INFO} "
                f"({DEFAULT_DOMAIN_PREIMAGE})"
            )

    def mine_forever(self):
        wallet = self.wallets[self.mining_node.tank]
        address = wallet.getnewaddress()
        interval = self.options.mine_interval
        self.log.info(
            f"Starting simple mining loop from {self.mining_node.tank}: "
            f"1 block every {interval} second(s)"
        )
        while True:
            try:
                self.generatetoaddress(
                    self.mining_node,
                    1,
                    address,
                    sync_fun=self.no_op,
                )
                self.log.info(
                    f"generated 1 block from {self.mining_node.tank}; "
                    f"height={self.mining_node.getblockcount()}"
                )
            except Exception as e:
                self.log.warning(f"simple mining loop failed: {e}")
            sleep(interval)

    def run_test(self):
        if not self.nodes:
            raise AssertionError("No tanks found")

        self.wait_for_tanks_connected()
        self.mining_node = self.select_mining_node()
        self.char_nodes = self.discover_char_nodes()

        wallet_nodes = [self.mining_node]
        wallet_nodes.extend(
            node
            for node in self.char_participants()
            if node.tank != self.mining_node.tank
        )
        self.wallets = {node.tank: self.ensure_miner(node) for node in wallet_nodes}

        self.ensure_source_funds(self.mining_node, self.wallets[self.mining_node.tank])
        self.fund_peer_wallets(
            self.mining_node,
            self.wallets[self.mining_node.tank],
            self.wallets,
        )
        self.create_bonds()
        self.activate_bonds()
        self.wait_for_active_bonds()
        self.schedule_domain()
        self.log.info(
            "CHAR app domain scheduled after active bonds. Sidecars now own app "
            "submission; scenario will only mine blocks."
        )
        self.mine_forever()


def main():
    CharSetup("").main()


if __name__ == "__main__":
    main()
