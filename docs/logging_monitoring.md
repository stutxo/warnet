# Logging and Monitoring

## Logging

### Pod logs

The command `warnet logs` will bring up a menu of pods to print log output from,
such as Bitcoin tanks, or scenario commanders. Follow the output with the `-f` option.

See command [`warnet logs`](/docs/warnet.md#warnet-logs)

### Bitcoin Core logs

Entire debug log files from a Bitcoin tank can be dumped by using the tank's
pod name.

Example:

```sh
$ warnet bitcoin debug-log tank-0000


2023-10-11T17:54:39.616974Z Bitcoin Core version v25.0.0 (release build)
2023-10-11T17:54:39.617209Z Using the 'arm_shani(1way,2way)' SHA256 implementation
2023-10-11T17:54:39.628852Z Default data directory /home/bitcoin/.bitcoin
... (etc)
```

See command [`warnet bitcoin debug-log`](/docs/warnet.md#warnet-bitcoin-debug-log)

### Aggregated logs from all Bitcoin nodes

Aggregated logs can be searched using `warnet bitcoin grep-logs` with regex patterns.

See more details in [`warnet bitcoin grep-logs`](/docs/warnet.md#warnet-bitcoin-grep-logs)

Example:

```sh
$ warnet bitcoin grep-logs 94cacabc09b024b56dcbed9ccad15c90340c596e883159bcb5f1d2152997322d

tank-0001: 2023-10-11T17:44:48.716582Z [miner] AddToWallet 94cacabc09b024b56dcbed9ccad15c90340c596e883159bcb5f1d2152997322d  newupdate
tank-0001: 2023-10-11T17:44:48.717787Z [miner] Submitting wtx 94cacabc09b024b56dcbed9ccad15c90340c596e883159bcb5f1d2152997322d to mempool for relay
tank-0001: 2023-10-11T17:44:48.717929Z [validation] Enqueuing TransactionAddedToMempool: txid=94cacabc09b024b56dcbed9ccad15c90340c596e883159bcb5f1d2152997322d wtxid=0cc875e73bb0bd8f892b70b8d1e5154aab64daace8d571efac94c62b8c1da3cf
tank-0001: 2023-10-11T17:44:48.718040Z [validation] TransactionAddedToMempool: txid=94cacabc09b024b56dcbed9ccad15c90340c596e883159bcb5f1d2152997322d wtxid=0cc875e73bb0bd8f892b70b8d1e5154aab64daace8d571efac94c62b8c1da3cf
tank-0001: 2023-10-11T17:44:48.723017Z [miner] AddToWallet 94cacabc09b024b56dcbed9ccad15c90340c596e883159bcb5f1d2152997322d
tank-0002: 2023-10-11T17:44:52.173199Z [validation] Enqueuing TransactionAddedToMempool: txid=94cacabc09b024b56dcbed9ccad15c90340c596e883159bcb5f1d2152997322d wtxid=0cc875e73bb0bd8f892b70b8d1e5154aab64daace8d571efac94c62b8c1da3cf
... (etc)
```


## Monitoring and Metrics

## Install logging infrastructure

If any tank in a network is configured with `collectLogs: true` or `metricsExport: true`
then the logging stack will be installed automatically when `warnet deploy` is executed.

The logging stack includes Loki, Prometheus, and Grafana. Together these programs
aggregate logs and data from Bitcoin RPC queries into a web-based dashboard.

## Connect to logging dashboard

The logging stack including the user interface web server runs inside the kubernetes cluster.
Warnet will forward port `2019` locally from the cluster, and the landing page for all
web based interfaces will be available at `localhost:2019`.

This page can also be opened quickly with the command [`warnet dashboard`](/docs/warnet.md#warnet-dashboard)


### Prometheus

To monitor RPC return values over time, a Prometheus data exporter can be connected
to any Bitcoin Tank and configured to scrape any available RPC results.

The `bitcoin-exporter` image is defined in `resources/images/exporter` and
maintained in the BitcoinDevProject dockerhub organization. To add the exporter
in the Tank pod with Bitcoin Core add the `metricsExport: true` value to the node in the yaml file.
For local exporter changes, build a local tag and point the tank chart at it:

```sh
docker build -t bitcoindevproject/bitcoin-exporter:char-debug resources/images/exporter
```

```yaml
metricsImage:
  repository: bitcoindevproject/bitcoin-exporter
  tag: char-debug
  pullPolicy: Never
```

The default metrics are defined in the `bitcoin-exporter` image:
- Block count
- Number of inbound peers
- Number of outbound peers
- Mempool size (# of TXs)

Metrics can be configured by setting an additional `metrics` value to the node in the yaml file. The metrics value is a space-separated list of labels, RPC commands with arguments, and
JSON keys to resolve the desired data:

```
label=method(arguments)[JSON result key][...]
```

Two helper prefixes are supported for values that are not direct gauges:

```
label=COUNT:method(arguments),key,value
label=HASH:method(arguments)[JSON result key][...]
label=CHAR_DOMAIN:domain_hex,domain_info,result_key
label=CHAR_DOMAIN_HASH:domain_hex,domain_info,result_key
label=CHAR_DOMAIN_INFO:domain_hex,domain_info
label=CHAR_DOMAIN_DECISION_HISTORY:domain_hex,domain_info,limit
label=CHAR_BONDS_INFO:active
```

`COUNT:` counts matching objects in an RPC result list. `HASH:` turns a string,
object, or list result into a stable numeric fingerprint so nodes can be compared
in Prometheus/Grafana while exact values stay in node logs. `CHAR_DOMAIN:` and
`CHAR_DOMAIN_HASH:` guard `getdomaininfo` behind `domain_registry("list")`, so
domain metrics stay empty until the scheduled domain exists. `CHAR_DOMAIN_INFO:`
exports exact string values such as current ballot, last confirmed ballot, and
decision roll hashes as metric labels for Grafana table panels. It also fetches
the latest decided roll so the table can show raw `data`, decoded `data_text`,
and `data_hash` together.
`CHAR_DOMAIN_DECISION_HISTORY:` exports the most recent decided rolls for a
domain, bounded by `limit`, so Grafana can show per-node roll history without
mixing it into the latest-state table.
`CHAR_BONDS_INFO:active` exports open `getallcharbonds(1)` rows as labels,
including bond txid, amount, and attestation summary fields.
The bundled bond visibility dashboard counts nodes by stable bond identity
only (`txid`, `issuer`, `amount`, `closed`) so differing latest attestation
summaries do not split one visible bond across multiple rows.

For example, the default metrics listed above would be explicitly configured as follows:

```yaml
nodes:
  - name: tank-0000
    metricsExport: true
    metrics: blocks=getblockcount() inbounds=getnetworkinfo()["connections_in"] outbounds=getnetworkinfo()["connections_out"] mempool_size=getmempoolinfo()["size"]
```

For Char Bitcoin networks created by `warnet new` or `warnet create`, Warnet
adds these metrics automatically when Grafana logging is enabled. For a manually
edited network, enable metrics on every tank and scrape the same scheduled
domain from each node. The default `char_setup.py` domain is
`edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090`
(`sha256("warnet")`):

```yaml
nodes:
  - name: tank-0000
    metricsExport: true
    metrics: >
      blocks=getblockcount()
      inbounds=getnetworkinfo()["connections_in"]
      outbounds=getnetworkinfo()["connections_out"]
      mempool_size=getmempoolinfo()["size"]
      char_domain_next_ballot=CHAR_DOMAIN:edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090,warnet,next_ballot
      char_domain_is_next_leader_mine=CHAR_DOMAIN:edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090,warnet,is_next_leader_mine
      char_domain_decision_roll_info=CHAR_DOMAIN_INFO:edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090,warnet
      char_domain_decision_roll_history=CHAR_DOMAIN_DECISION_HISTORY:edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090,warnet,20
      char_active_bond_info=CHAR_BONDS_INFO:active
```

Repeat the same `metrics` block for each tank you want on the Char Domain State
dashboard if you are not using the generated Char defaults.

The Char Domain State dashboard shows each node's `next_ballot` value, the
latest decided roll reported by `getdomaininfo()`, and a roll-group table
grouped by `last_decided_ballot`, `resolution_type`, `roll_hash`, raw `data`,
decoded `data_text`, `data_hash`, and `zeitgeist`. Impossible rolls are shown
with `resolution_type=impossible`; empty or unresolved/null rolls have blank
data labels. The exporter does not synthesize decided ballots from bond
attestation height; when `latest_decided_ballot` is not available, the
latest-roll labels stay blank and the history table has no rows for that node.
Different rows mean nodes are currently reporting different decision rolls, not
that Warnet has failed the scenario. A separate history table shows the last
twenty decided rolls each node reports, including raw `data`, decoded
`data_text`, and `data_hash`.

The bundled `resources/scenarios/char_setup.py` scenario intentionally does not
observe or submit app data. It bootstraps funds, waits for active bonds,
schedules the domain, then mines one block every 30 seconds. Per-node app
behavior is visible through the dashboard and sidecar logs.

For CHAR app testing, generated CHAR networks enable a `char-app` sidecar on
each tank. The app listens to local `leader` and `decisionroll` ZMQ topics,
verifies finalized rolls through RPC, keeps in-memory counter state, and submits
`warnet-counter:<n>` when its local node receives a matching leader hint. On
startup every sidecar also attempts ballot 0 once with `addreferendumvote` mode
`init`, so the first ballot does not depend on catching the initial leader ZMQ
notification. The node's `charattestintervalms` setting drives attestation; the
sidecar does not call `attestbonds` manually. Decision-roll catch-up calls
`getreferendumresolution` from the local cursor and advances only in ballot
order. Empty and impossible ballots advance the app cursor without changing the
counter.
The metrics exporter remains read-only. Sidecars emit
`CHAR_APP_EVENT` for accepted leader votes and `CHAR_APP_BUG_EVIDENCE` when a
locally accepted vote finalizes as a different, empty, or impossible decision.

The data can be retrieved directly from the Prometheus exporter container in the tank pod via port `9332`, example:

```
# HELP blocks getblockcount()
# TYPE blocks gauge
blocks 704.0
# HELP inbounds getnetworkinfo()["connections_in"]
# TYPE inbounds gauge
inbounds 0.0
# HELP outbounds getnetworkinfo()["connections_out"]
# TYPE outbounds gauge
outbounds 0.0
# HELP mempool_size getmempoolinfo()["size"]
# TYPE mempool_size gauge
mempool_size 0.0
```

### Defining lnd metrics to capture

Lightning nodes can also be configured to export metrics to prometheus using `lnd-exporter`.
Example configuration is provided in `test/data/ln/`. Review `node-defauts.yaml` for a typical logging configuration. All default metrics reported to prometheus are prefixed with `lnd_`

[lnd-exporter configuration reference](https://github.com/bitcoin-dev-project/lnd-exporter/tree/main?tab=readme-ov-file#configuration)
lnd-exporter assumes same macaroon referenced in ln_framework (can be overridden by env variable)

**Note: `test/data/ln` and `test/data/logging` take advantage of **extraContainers** configuration option to add containers to default `lnd/templates/pod`*

### Grafana

Data from Prometheus exporters is collected and fed into Grafana for a
web-based interface.

#### Dashboards

Grafana dashboards are described in JSON files. A default Warnet dashboard
is included and any other json files in the `/resources/charts/grafana-dashboards/files` directory
will also be deployed to the web server. The Grafana UI itself also has an API
for creating, exporting, and importing other dashboard files.
