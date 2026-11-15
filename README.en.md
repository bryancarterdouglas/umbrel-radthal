# Umbrel-radthal: Radthal for Umbrel

🇫🇷 [Version française](README.md)

An [Umbrel](https://umbrel.com) app store for **[Radthal](https://github.com/bryancarterdouglas/radthal)**, an experimental Bitcoin Core fork: see the main repository for what it is, its rules (including the creator's 2% share from block 25,000) and its warnings.

## The "Radthal" app: one-click install

A **self-contained** app: a single "Install" button, with nothing to compile or copy onto the Umbrel. It runs:

- the Radthal **node**;
- the **ckpool** mining server (solo mode), so you can connect a SHA-256 miner (Avalon, Antminer, Bitaxe...);
- all the **pages**, behind a menu bar and protected by the Umbrel password: Node, Dashboard, Mempool, ckpool, Wallet and Timestamping.

Install **Radthal** (not "Radthal Node") from this store (see [Add the store to Umbrel](#add-the-store-to-umbrel)).

**Mining.** Point your miner at port **3333** of the Umbrel, with your `rdt1...` address as the user name: the **ckpool** page shows the settings. This port is meant for your local network only: never forward it from your router.

**What was tested.** On a Raspberry Pi 5 running Umbrel: install, node sync, mining with an Avalon (one block found), wallet and a reboot of the Pi. Images also exist for amd64, but that is untested on Umbrel. Radthal remains an experimental project, with no guaranteed value.

**Before installing.** Ports 9333 (network) and 3333 (mining) can only be used once: if you already have a Radthal node on this Umbrel (the older separate apps "Radthal Node", "Radthal Wallet"...), or a `ckpool` started by hand, stop them first.

### Reusing an existing wallet

The app creates its own wallet, `monportefeuille`, empty at first. To reuse the wallet of an old node (a folder containing `wallet.dat`): **back it up**, stop the old node, then copy its folder into the app's folder under that name:

```bash
sudo mkdir -p ~/umbrel/app-data/radthal-one/data/wallets
sudo cp -a /path/to/your/old/wallet ~/umbrel/app-data/radthal-one/data/wallets/monportefeuille
sudo chown -R 1000:1000 ~/umbrel/app-data/radthal-one/data/wallets
```

Then open the **Wallet** page and click "Créer mon portefeuille" (create my wallet): the existing wallet is reloaded. Do not run the old node with the same wallet at the same time: two live copies would end up contradicting each other.

## The app's pages

- **Node**: node status (blocks, sync, peers shown with a nickname, never their address, ports).
- **Dashboard**: overview: latest blocks, fork monitoring, difficulty, network power, miner.
- **Mempool**: pending transactions, fee distribution, latest blocks.
- **ckpool**: stats of the solo pool: miners, hashrate, shares, a miner's connection settings.
- **Wallet**: balance, receiving address with QR code, sending RDT (preview then confirmation), history, backup, short messages written in the chain.
- **Timestamping**: prove that a document existed at the date of a block (the SHA-256 fingerprint is computed in the browser; the document never leaves your device).

The first four pages are **read-only**. Wallet and Timestamping **act**: the keys stay in the node, the pages never see them. A message written in the chain is **public and permanent**.

| Dashboard | ckpool | Node |
|:---:|:---:|:---:|
| <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-tableau-de-bord.jpg" width="260" alt="Radthal dashboard"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-ckpool.jpg" width="260" alt="ckpool page"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-noeud.jpg" width="260" alt="Node page"> |

*(Screenshots of a running node: balance hidden, local network address replaced.)*

## Add the store to Umbrel

1. Open Umbrel's **App Store**.
2. Click the **three dots** at the top right, then **Community App Stores**.
3. Paste `https://github.com/bryancarterdouglas/umbrel-radthal` and confirm.

Community stores are not vetted by the Umbrel team: only add stores from developers you trust, and read the code (it is short, in the `radthal-one` folder).

## Security

- The pages are **plain HTTP, on the local network**: **never** expose them to the Internet.
- Never open the node's RPC (or ckpool's Stratum port) to the Internet. To help the network, only port **9333** (P2P) is forwarded, and only for the node itself.
- Never show your wallet address, its QR code or your balance in public screenshots.

## Problem or question

Open an "issue" on this repository. Radthal is an experimental project, with no warranty, no promised value and no connection with Bitcoin or its developers.

## License

MIT (see `LICENSE`).
