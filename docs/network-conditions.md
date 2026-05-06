# Network Conditions

Warnet can apply deterministic random latency between configured tank peers with
Linux `tc netem`. The feature is enabled from `network.yaml` and applies only to
tank-to-tank `addnode` links.

```yaml
networkConditions:
  enabled: true
  seed: 1337
  mode: random-latency
  scope: addnode-edges
  latency:
    minRttMs: 20
    maxRttMs: 600
    jitterPct: 10
    distribution: normal
```

RTT values are sampled once per undirected edge from the configured range. Warnet
then applies half the RTT as one-way egress delay in each direction, with jitter
derived from `jitterPct`. The same topology and seed generate the same latency
assignment, which makes failures reproducible.

Warnet adds a `netem` sidecar to each tank when network conditions are enabled.
The sidecar has `NET_ADMIN` and `NET_RAW`, installs `iproute2` and `iputils` by
default, and keeps running so Warnet can apply and inspect `tc` rules. The
Bitcoin container and image are unchanged.

Verify the active rules:

```sh
kubectl exec tank-0000 -c netem -- tc -s qdisc show dev eth0
```

Measure latency to a peer pod:

```sh
kubectl get pod tank-0001 -o jsonpath='{.status.podIP}'
kubectl exec tank-0000 -c netem -- ping -c 5 <peer-pod-ip>
```

The default sidecar uses `alpine:3.20` and installs packages at startup. For
clusters without pod egress, build the bundled image and override the sidecar
image in `node-defaults.yaml`:

```sh
docker build -t bitcoindevproject/netem:local resources/images/netem
```

```yaml
netem:
  image:
    repository: bitcoindevproject/netem
    tag: local
    pullPolicy: Never
  setupCommand: ""
```
