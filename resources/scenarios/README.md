# Scenario Setup Guide

## CHAR Setup

Use `char_setup.py` after the network is deployed and all tanks are running. The
scenario uses `tank-0000` as the mining/funding source; every tank with
`charbondindex` enabled is treated as a CHAR participant.

```sh
mkdir ../warnet_data
warnet new ../warnet_data/char
warnet deploy ../warnet_data/char/networks/<network_name>
warnet run resources/scenarios/char_setup.py
warnet dashboard
```

The setup scenario is intentionally small:

1. Wait for tank peers to connect.
2. Create or load a `miner` wallet on `tank-0000` and each CHAR tank.
3. Mine enough mature coinbase funds on `tank-0000` when needed.
4. Fund each CHAR tank wallet.
5. Create one CHAR bond per CHAR tank unless the tank already owns a bond.
6. Mine to the active CHAR stake snapshot.
7. Wait until every CHAR tank sees the active bond set.
8. Schedule the default `warnet` app domain.
9. Mine one block every 30 seconds.

The scenario does not submit app data and does not manually call `attestbonds`.
Each `char-app` sidecar subscribes to local `leader` and `decisionroll` ZMQ
events, verifies with local RPC, initializes ballot 0 once with
`addreferendumvote` mode `init`, submits later ballots only from matching leader
ZMQ hints, and advances its in-memory counter state from finalized decision
rolls.

Impossible and empty rolls advance the sidecar's local ballot cursor while
leaving the counter unchanged. Unresolved rolls stop catch-up until the node
returns a finalized result. Counter decisions use `warnet-counter:<n>` as
hex-encoded payloads.

If you are testing local exporter changes for the CHAR Domain State dashboard,
build a local exporter tag and set it in the network defaults before deploying:

```sh
docker build -t bitcoindevproject/bitcoin-exporter:char-debug resources/images/exporter
```

```yaml
metricsImage:
  repository: bitcoindevproject/bitcoin-exporter
  tag: char-debug
  pullPolicy: Never
```

If you are testing local CHAR app sidecar changes, build a local app tag and set
it in the network defaults before deploying:

```sh
docker build -t bitcoindevproject/char-app:char-debug resources/images/char-app
```

```yaml
charApp:
  enabled: true
  image:
    repository: bitcoindevproject/char-app
    tag: char-debug
    pullPolicy: Never
  decisionScanWindow: 8
```

Each `char-app` sidecar emits `CHAR_APP_EVENT` for accepted and finalized local
votes. Decision-roll hints reconcile by calling `getreferendumresolution`
forward from local state instead of trusting `getdomaininfo` as the source of the
latest ballot. The sidecar commits only finalized results in ballot order. If a
locally accepted vote finalizes differently, the sidecar emits
`CHAR_APP_BUG_EVIDENCE`.

Capture useful repro logs with:

```sh
warnet logs -f | tee /tmp/char-app-repro.log
rg "CHAR_APP_BUG_EVIDENCE|CHAR_APP_EVENT" /tmp/char-app-repro.log
```

The default domain is `warnet`, whose preimage hex is:

```text
edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090
```

Useful flag:

```sh
warnet run resources/scenarios/char_setup.py --mine-interval 120
```
