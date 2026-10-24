# Umbrel-radthal: Umbrel pages for a Radthal node

🇫🇷 [Version française](README.md)

Six small web apps for [Umbrel](https://umbrel.com) that let you **view and use a [Radthal](https://github.com/bryancarterdouglas/radthal) node** without the command line. Radthal is an experimental Bitcoin Core fork: see the main repository for what it is, its rules (including the creator's 2% share from block 25,000) and its warnings.

> **This is not a one-click install.** These apps were made for the author's Raspberry Pi: they read files at specific locations on that Pi (see [What the apps assume](#what-the-apps-assume)). If you simply want to **run a node**, follow the README of [radthal](https://github.com/bryancarterdouglas/radthal) (Docker, no Umbrel needed; there is an [English version](https://github.com/bryancarterdouglas/radthal/blob/main/README.en.md)). These pages are an extra comfort for people who already have an Umbrel and a Radthal node.

## The apps

| App | Port | What it does |
|---|---|---|
| **Radthal Node** | 3777 | runs the node (`bitcoind`) from Umbrel and shows its status: blocks, difficulty, peers, ports. **Stopping the app stops the node.** |
| **Radthal Dashboard** | 3778 | overview: latest blocks, fork monitoring, difficulty, network power, miner |
| **Radthal Mempool** | 3779 | pending transactions, fee distribution, latest blocks |
| **Radthal ckpool** | 3780 | stats of the ckpool solo pool: miners, hashrate, shares, connection settings for an ASIC |
| **Radthal Wallet** | 3781 | balance, receiving address with QR code, sending RDT (preview then confirmation), history, backup, short messages written in the chain |
| **Radthal Horodatage** (timestamping) | 3782 | prove that a document existed at the date of a block (the SHA-256 fingerprint is computed in the browser; the document never leaves your device) |

The first four pages are **read-only**. Wallet and Horodatage **act**: the keys stay in the node, the pages never see them. A message written in the chain is **public and permanent**.

| Dashboard | ckpool | Node |
|:---:|:---:|:---:|
| <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-tableau-de-bord.jpg" width="260" alt="Radthal dashboard"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-ckpool.jpg" width="260" alt="ckpool page"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-noeud.jpg" width="260" alt="Node page"> |

*(Screenshots of a running node: balance hidden, local network address replaced.)*

## Add the store to Umbrel

1. Open Umbrel's **App Store**.
2. Click the **three dots** at the top right, then **Community App Stores**.
3. Paste `https://github.com/bryancarterdouglas/Umbrel-radthal` and confirm.

Community stores are not vetted by the Umbrel team: only add stores from developers you trust, and read the code (it is short, in the `radthal-*` folders).

## What the apps assume

The pages talk to the **node's RPC** and read a few files on the Pi. On the author's Pi:

| Item | Location / value |
|---|---|
| Radthal node with its RPC | on the host machine, port **9332**, user `radthal` |
| RPC password | file `/home/umbrel/radthal/rpcpass.txt` (mounted read-only) |
| Node programs (for **Radthal Node**) | compiled in `/home/umbrel/radthal/build`, data in `/home/umbrel/radthal-data` |
| Node image (for **Radthal Node**) | `localhost:5000/radthal-build:v1`, served by a small local registry on the Pi (Umbrel always downloads images) |
| ckpool logs (for **ckpool** and **Dashboard**) | `/home/umbrel/radthal/ckpool/logs` |
| Node wallet (for **Wallet**) | named `minage` |
| Certificates folder (for **Horodatage**) | `/home/umbrel/radthal/horodatage` |

If your layout is different, adapt these paths in each app's `docker-compose.yml`.

## Security

- The pages are **plain HTTP, on the local network**: **never** expose them to the Internet.
- Never open the node's RPC (or ckpool's Stratum port) to the Internet. To help the network, only port **9333** (P2P) is forwarded, and only for the node itself.
- Never show your wallet address, its QR code or your balance in public screenshots.

## Problem or question

Open an "issue" on this repository. Radthal is an experimental project, with no warranty, no promised value and no connection with Bitcoin or its developers.

## License

MIT (see `LICENSE`).
