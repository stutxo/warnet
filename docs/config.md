# Configuration value propagation

This flowchart illustrates the process of how values for the Bitcoin Core module are handled and deployed using Helm in a Kubernetes environment.

The process is similar for other modules (e.g. fork-observer), but may differ slightly in filenames.

- The process starts with the `values.yaml` file, which contains default values for the Helm chart.
- There's a decision point to check if user-provided values are available.
  These are found in the following files:
    - For config applied to all nodes: `<network_name>/node-defaults.yaml`
    - For network and per-node config: `<network_name>/network.yaml`

> [!TIP]
> `values.yaml` can be overridden by `node-defaults.yaml` which can be overridden in turn by `network.yaml`.

- If user-provided values exist, they override the defaults from `values.yaml`. If not, the default values are used.
- The resulting set of values (either default or overridden) becomes the final set of values used for deployment.
- These final values are then passed to the Helm templates.
- The templates (`configmap.yaml`, `service.yaml`, `servicemonitor.yaml`, and `pod.yaml`) use these values to generate the Kubernetes resource definitions.
- Helm renders these templates, substituting the values into the appropriate places.
- The rendering process produces the final Kubernetes manifest files.
- Helm then applies these rendered manifests to the Kubernetes cluster.
- Kubernetes processes these manifests and creates or updates the corresponding resources in the cluster.
- The process ends with the resources being deployed or updated in the Kubernetes cluster.

In the flowchart below, boxes with a red outline represent default or user-supplied configuration files, blue signifies files operated on by Helm or Helm operations, and green by Kubernetes.

```mermaid
graph TD
    A[Start]:::start --> B[values.yaml]:::config
    subgraph User Configuration [User configuration]
        C[node-defaults.yaml]:::config
        D[network.yaml]:::config
    end
    B --> C
    C -- Bottom overrides top ---D
    D --> F[Final values]:::config
    F --> I[Templates]:::helm
    I --> J[configmap.yaml]:::helm
    I --> K[service.yaml]:::helm
    I --> L[servicemonitor.yaml]:::helm
    I --> M[pod.yaml]:::helm
    J --> N[Helm renders templates]:::helm
    K & L & M --> N
    N --> O[Rendered kubernetes
    manifests]:::helm
    O --> P[Helm applies manifests to 
    kubernetes]:::helm
    P --> Q["Kubernetes 
    creates/updates resources"]:::k8s
    Q --> R["Resources 
    deployed/updated in cluster"]:::finish

    classDef start fill:#f9f,stroke:#333,stroke-width:4px
    classDef finish fill:#bbf,stroke:#f66,stroke-width:2px,color:#fff,stroke-dasharray: 5 5
    classDef config stroke:#f00
    classDef k8s stroke:#0f0
    classDef helm stroke:#00f
```

Users should only concern themselves therefore with setting configuration in the `<network_name>/[network|node-defaults].yaml` files.

## Per-node network latency

The Bitcoin Core chart supports `extraContainers`, so a network can add a
`NET_ADMIN` sidecar that configures Linux `tc netem` in the pod network
namespace. Put the sidecar in `node-defaults.yaml` to apply the same delay to
every tank, or put it on individual nodes in `network.yaml` when each tank needs
a different delay:

```yaml
nodes:
  - name: tank-0000
    addnode:
      - tank-0001
    extraContainers:
      - name: netem
        image: alpine:3.20
        securityContext:
          capabilities:
            add: [NET_ADMIN, NET_RAW]
        command: ["/bin/sh", "-c"]
        args:
          - |
            apk add --no-cache iproute2 iputils
            tc qdisc replace dev eth0 root netem delay 25ms
            while true; do sleep 3600; done

  - name: tank-0001
    addnode:
      - tank-0000
    extraContainers:
      - name: netem
        image: alpine:3.20
        securityContext:
          capabilities:
            add: [NET_ADMIN, NET_RAW]
        command: ["/bin/sh", "-c"]
        args:
          - |
            apk add --no-cache iproute2 iputils
            tc qdisc replace dev eth0 root netem delay 150ms
            while true; do sleep 3600; done
```

This shapes traffic leaving each tank pod. A ping between two tanks is delayed
in both directions, so the observed round-trip time is approximately the sum of
the two configured delays. In the example above, pings between `tank-0000` and
`tank-0001` should be roughly `175ms` before normal cluster overhead.
