from test_framework.messages import sha256, ser_compact_size


class CharUtil:
    """Utility wrapper around Char-specific RPCs to make tests concise."""

    def __init__(self, node, test_framework):
        self._node = node
        self._test_framework = test_framework

    def create_bond(
        self,
        amount,
        return_tx: bool = False,
        duration=None,
        wait_for_indexes: bool = True,
        stake_amount=None,
        generate_block: bool = True,
        auto_mine_if_needed: bool = False,
    ):
        """Create a Char bond and optional stake output, then mine a block so they confirm.

        If stake_amount is None, funds a stake output equal to `amount`.
        If stake_amount is 0 or negative, no stake output is created.
        If generate_block is False, the bond stays unconfirmed until the caller mines blocks.
        If auto_mine_if_needed is True, mine to satisfy wallet balance requirements.

        Returns the bond transaction txid.
        """
        wallet_name = self._node.listwallets()[0]
        wallet = self._node.get_wallet_rpc(wallet_name)

        # Ensure the wallet has enough confirmed balance to fund the bond.
        # Functional tests should prefund explicitly to avoid hidden slow paths.
        req_balance = (stake_amount if stake_amount and stake_amount > 0 else amount) + 2  # extra safety margin for fees
        confirmed_balance = wallet.getbalance("*", 1, False)
        if confirmed_balance < req_balance:
            if auto_mine_if_needed:
                while confirmed_balance < req_balance:
                    blocks_to_generate = 21
                    self._test_framework.generate_and_wait(self._node, blocks_to_generate)
                    confirmed_balance = wallet.getbalance("*", 1, False)
            else:
                raise AssertionError(
                    f"Insufficient confirmed balance for create_bond: balance={confirmed_balance}, "
                    f"required>={req_balance}. Prefund wallet or pass auto_mine_if_needed=True."
                )

        tx = wallet.walletcreatetaprootoutputforcharbond()
        if stake_amount is None:
            stake_amount = amount
        if stake_amount and stake_amount > 0:
            if duration is not None:
                wallet.fundcharstake(tx["txid"], stake_amount, duration)
            else:
                wallet.fundcharstake(tx["txid"], stake_amount)
        if generate_block:
            blockhash = self._node.generate(1, called_by_framework=True)[0]
            tx_info = self._node.getrawtransaction(tx["txid"], True, blockhash)
            assert tx_info["in_active_chain"], "Bond transaction is not in the active chain"
        if wait_for_indexes and generate_block:
            # Wait for indexes to catch up after creating bond
            self._test_framework.wait_char_indexes_caught_up()
        if return_tx:
            return tx
        return tx["txid"]

    def create_bonds(
        self,
        amounts,
        *,
        stake_amounts=None,
        wait_for_indexes: bool = True,
        confirm_in_single_block: bool = True,
    ):
        """Create multiple bonds; optionally batch-confirm them in one block.

        Returns list of txids in the given order.
        """
        if stake_amounts is None:
            stake_amounts = [None] * len(amounts)
        assert len(stake_amounts) == len(amounts)

        txids = []
        for amount, stake_amount in zip(amounts, stake_amounts):
            txids.append(
                self.create_bond(
                    amount,
                    stake_amount=stake_amount,
                    generate_block=False,
                    wait_for_indexes=False,
                )
            )

        if confirm_in_single_block and txids:
            self._test_framework.generate_and_wait(
                self._node,
                1,
                wait_for_indexes=wait_for_indexes,
            )
        return txids

    def all_bonds(self):
        return self._node.getallcharbonds()

    def attest_bonds_and_wait(self, *, async_: bool = False, wait_for_global_indexes: bool = True):
        """Call attestbonds RPC and wait for indexes to sync.

        Args:
            async_: Passed through to attestbonds.
            wait_for_global_indexes: If True, wait for the full test network's CHAR
                indexes to converge. Split-network tests can disable this and rely
                only on local ballot visibility.

        Returns:
            Result of attestbonds RPC call.
        """
        kwargs = {"async": True} if async_ else {}
        res = self._node.attestbonds(**kwargs)

        if async_:
            return res

        def _attestations_visible():
            for entry in res:
                attestation_query_id = entry.get("chain_id") or entry.get("txid") or entry.get("bond_id")
                ballot_number = entry.get("ballot_number")
                char_hash = entry.get("char_hash")
                if not attestation_query_id or ballot_number is None or not char_hash:
                    return False
                try:
                    att = self._node.getattestationforbondatballot(
                        attestation_query_id,
                        ballot_number,
                    )
                except Exception:
                    return False
                if att.get("char_hash") != char_hash:
                    return False
                expected_block_hash = entry.get("block_hash")
                if expected_block_hash is not None and att.get("block_hash") != expected_block_hash:
                    return False
            return True

        # If attestbonds returned attestations, wait until they are queryable via the
        # ballot index. When there are no attestations, this returns immediately.
        self._test_framework.wait_until(_attestations_visible, timeout=60)
        if wait_for_global_indexes:
            self._test_framework.wait_char_indexes_caught_up()
        return res

    # Protocol limit copied from char constants for testing large attestations
    MAX_CHAR_BAMBOO_SIZE = 3_000_000

    def build_value_hex_of_size(self, num_bytes: int, fill_byte: int = 0x00) -> str:
        """Return a hex string representing exactly num_bytes of data.

        The returned string consists of a repeated single byte (default 0x00).
        """
        assert 0 <= fill_byte <= 0xFF
        if num_bytes <= 0:
            return ""
        b = f"{fill_byte:02x}"
        return b * num_bytes

    def build_max_bamboo_value_hex(self, *, fill_byte: int = 0x00, safety_bytes: int = 128) -> str:
        """Return a raw payload hex string that stays under the bamboo limit after internal vote wrapping.

        Internal referendum-vote wrapping is:
        CompactSize(leaf_type) + CompactSize(ballot_number) + CompactSize(len(data)) + data.
        To stay safely under the protocol limit for any ballot number, budget the
        worst-case overhead for the fixed leaf tag, a uint64 ballot number, and a
        payload length near MAX_CHAR_BAMBOO_SIZE.

        We subtract an additional safety margin to guarantee we remain under the limit.
        """
        # Worst-case overhead:
        # - leaf_type: 1 byte (CompactSize(LeafType::REFERENDUM_VOTE))
        # - ballot_number: 9 bytes (CompactSize uint64_t)
        # - payload len: 5 bytes (0xFE + 4 bytes) near 3MB
        vote_overhead = (
            len(ser_compact_size(0))
            + len(ser_compact_size((1 << 64) - 1))
            + len(ser_compact_size(self.MAX_CHAR_BAMBOO_SIZE))
        )
        payload_bytes = self.MAX_CHAR_BAMBOO_SIZE - vote_overhead - max(safety_bytes, 0)
        # Guardrails
        if payload_bytes < 1024:
            payload_bytes = 1024
        return self.build_value_hex_of_size(payload_bytes, fill_byte)

    def __getattr__(self, item):
        return getattr(self._node, item)

def str_to_hex(s: str) -> str:
    """Convert a string to its hexadecimal representation."""
    return sha256(s.encode("utf-8")).hex()
