# Scenario Setup Guide

## Char Setup

Use `char_setup.py` after the network is deployed and all tanks are running.

```sh
mkdir ../warnet_data
warnet new ../warnet_data/char
warnet deploy ../warnet_data/char/networks/<network_name>
warnet run resources/scenarios/char_setup.py
warnet dashboard
```

The setup scenario does the full Char bootstrap:

1. Waits for all tank peers to connect.
2. Creates or loads a `miner` wallet on each tank.
3. Mines enough mature coinbase funds on tank 0 when needed.
4. Funds every other tank wallet.
5. Creates one Char bond per tank unless the tank already owns a bond.
6. Mines to the active Char stake snapshot.
7. Schedules the default `warnet` app domain.
8. Submits and verifies a dummy app payload.
9. Keeps mining one block every 30 seconds so the network continues advancing.

Common options:

```sh
# Run setup, then exit instead of continuing to mine.
warnet run /path/to/scenarios/char_setup.py --no-continuous-mining

# Mine every 10 seconds after setup instead of every 30 seconds.
warnet run /path/to/scenarios/char_setup.py --mine-interval 10

# Recreate bonds even when existing owned bonds are found.
warnet run /path/to/scenarios/char_setup.py --force-new-bonds

# Skip the dummy app vote/resolution check.
warnet run /path/to/scenarios/char_setup.py --skip-dummy-app
```

The default domain is `warnet`, whose preimage hex is:

```text
edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090
```

Run `warnet run /path/to/scenarios/char_setup.py --help` for the full option list.
