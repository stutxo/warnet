![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https://raw.githubusercontent.com/bitcoin-dev-project/warnet/main/pyproject.toml)
# Warnet

Monitor and analyze the emergent behaviors of Bitcoin networks.

## Major Features

* Launch a bitcoin network with a specified number of nodes connected to each other according to a network topology.
* Run scenarios of network behavior across the network which can be programmed using the Bitcoin Core functional [test_framework language](https://github.com/bitcoin/bitcoin/tree/master/test/functional).
* Collect and search data from nodes including log files and p2p messages.
* Monitor and visualize performance data from Bitcoin nodes.
* Connect to a large network running in a remote cluster, or a smaller network running locally.
* Add a Lightning Network with its own channel topology and payment activity.

## Documentation

- [Design](/DESIGN.md)
- [Installation](/docs/install.md)
- [CLI Commands](/docs/warnet.md)
- [Network configuration with yaml files](/docs/config.md)
- [Plugins](/docs/plugins.md)
- [Scenarios](/docs/scenarios.md)
- [Monitoring](/docs/logging_monitoring.md)
- [Snapshots](/docs/snapshots.md)
- [Connecting to local nodes outside the cluster](/docs/connecting-local-nodes.md)
- [Scaling](/docs/scaling.md)
- [Contributing](/docs/developer-notes.md)


## Quick Start

### 1. Create a python virtual environment

```sh
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Warnet

```sh
pip install warnet
```

### 3. Set up dependencies

Warnet will ask which back end you want to use, check that it is working,
and install additional client tools into the virtual environment.

```sh
warnet setup
```

### 4. Build the Char Bitcoin image

Build `judica-org/char-bitcoin` from `master` and load it into your local
Docker environment:

```sh
warnet image build \
  --repo judica-org/char-bitcoin \
  --commit-sha master \
  --tags judica-org/char-bitcoin:30.0.0 \
  --arches arm64 \
  --action load \
  --ssh \
  --build-jobs 4
```

Use `--arches amd64` on Intel Linux, or omit `--arches` for the default
multi-arch build. The `30.0.0` value is the local Docker image tag; the source
revision above is `master`.

### 5. Create a project and network

Warnet will create a new folder structure containing standard scenario and plugin
files, and prompt for details about a network topology to create. Topology details
include number of Bitcoin nodes, which release versions or custom images to deploy
and how many random graph connections to start each node with.

```sh
warnet new /my/work/stuff/projectname
```

When prompted for the Bitcoin node version, choose `other` and enter:

```text
judica-org/char-bitcoin:30.0.0
```

Enable Grafana logging when prompted so `warnet dashboard` has metrics.

Then append Char runtime config under the existing `defaultConfig` block in the
generated `node-defaults.yaml`:

```yaml
  charenable=1
  charbatchverifyminconnseconds=0
  debug=char
```

### 6. Deploy the network

```sh
warnet deploy /my/work/stuff/projectname/networks/networkname
```

### 7. Run experiments

Set up one Char bond per node and schedule the default `warnet` app domain.
The default domain hex is `sha256("warnet")`:
`edfba5f37483dac7484bed0b573e85b88051dbe445665ffd27fbcb742adbb090`.

```sh
warnet run /my/work/stuff/projectname/scenarios/char_setup.py
```

For example, you can start mining blocks...

```sh
warnet run /my/work/stuff/projectname/scenarios/miner_std.py
```

... and then observe network connectivity and statistics in your browser:

```sh
warnet dashboard
```

### 8. Shut down the network

```sh
warnet down
```

### 9. Customize

Read the docs and learn how to write your own [scenarios](docs/scenarios.md)
or add [plugins](docs/plugins.md) to your network. [Configure](docs/config.md) individual nodes
in the network by editing the `network.yaml` file or configure
defaults for all nodes in the network by editing `node-defaults.yaml`. Once
your network is running use Warnet [CLI](docs/warnet.md) commands to interact with it.


![warnet-art](https://raw.githubusercontent.com/bitcoin-dev-project/warnet/main/docs/machines.webp)
