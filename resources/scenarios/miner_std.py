#!/usr/bin/env python3

from time import sleep

from commander import Commander


class MinerStd(Commander):
    def set_test_params(self):
        # This is just a minimum
        self.num_nodes = 1

    def add_options(self, parser):
        parser.description = "Generate blocks over time"
        parser.usage = "warnet run /path/to/miner_std.py [options]"
        parser.add_argument(
            "--allnodes",
            dest="allnodes",
            action="store_true",
            help="When true, generate blocks from all nodes instead of just nodes[0]",
        )
        parser.add_argument(
            "--interval",
            dest="interval",
            default=60,
            type=int,
            help="Number of seconds between block generation (default 60 seconds)",
        )
        parser.add_argument(
            "--mature",
            dest="mature",
            action="store_true",
            help="When true, generate 101 blocks ONCE per miner",
        )
        parser.add_argument(
            "--tank",
            dest="tank",
            type=str,
            help="Select one tank by name as the only miner",
        )

    def run_test(self):
        self.log.info("Starting miners.")
        if self.options.tank:
            miner_nodes = [self.tanks[self.options.tank]]
        elif self.options.allnodes:
            miner_nodes = self.nodes
        else:
            miner_nodes = self.nodes[:1]

        miners = []
        for node in miner_nodes:
            wallet = self.ensure_miner(node)
            miners.append((node, wallet.getnewaddress()))

        num = 101 if self.options.mature else 1
        while True:
            for node, addr in miners:
                try:
                    self.generatetoaddress(node, num, addr, sync_fun=self.no_op)
                    height = node.getblockcount()
                    self.log.info(
                        f"generated {num} block(s) from node {node.index}. New chain height: {height}"
                    )
                except Exception as e:
                    self.log.error(f"node {node.index} error: {e}")
                sleep(self.options.interval)
            num = 1


def main():
    MinerStd("").main()


if __name__ == "__main__":
    main()
