# Umbrel-radthal : pages Umbrel pour un nœud Radthal

Six petites applications web pour [Umbrel](https://umbrel.com) qui permettent de **voir et d'utiliser un nœud [Radthal](https://github.com/bryancarterdouglas/radthal)** sans ligne de commande. Radthal est un fork expérimental de Bitcoin Core : voir le dépôt principal pour ce que c'est, ses règles (dont la part de 2 % du créateur à partir du bloc 25 000) et ses avertissements.

> **Ce n'est pas une installation en un clic.** Ces applications ont été faites pour le Raspberry Pi de l'auteur : elles lisent des fichiers à des emplacements précis de ce Pi (voir [Ce que les applications supposent](#ce-que-les-applications-supposent)). Si tu veux simplement **faire tourner un nœud**, suis le README de [radthal](https://github.com/bryancarterdouglas/radthal) (Docker, sans Umbrel). Ces pages sont un confort en plus, pour qui a déjà un Umbrel et un nœud Radthal.

## Les applications

| Application | Port | Rôle |
|---|---|---|
| **Radthal Node** | 3777 | lance le nœud (`bitcoind`) depuis Umbrel et affiche son état : blocs, difficulté, pairs, ports. **Arrêter l'application arrête le nœud.** |
| **Radthal Dashboard** | 3778 | vue d'ensemble : derniers blocs, surveillance des forks, difficulté, puissance du réseau, mineur |
| **Radthal Mempool** | 3779 | transactions en attente, répartition des frais, derniers blocs |
| **Radthal ckpool** | 3780 | statistiques du pool solo ckpool : mineurs, hashrate, parts, réglages de connexion d'un ASIC |
| **Radthal Wallet** | 3781 | solde, adresse de réception avec QR code, envoi de RDT (aperçu puis confirmation), historique, sauvegarde, messages courts écrits dans la chaîne |
| **Radthal Horodatage** | 3782 | prouver qu'un document existait à la date d'un bloc (l'empreinte SHA-256 est calculée dans le navigateur ; le document ne quitte jamais ton appareil) |

Les quatre premières pages **regardent** seulement. Wallet et Horodatage **agissent** : les clés restent dans le nœud, les pages ne les voient jamais. Un message écrit dans la chaîne est **public et définitif**.

| Tableau de bord | ckpool | Nœud |
|:---:|:---:|:---:|
| <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-tableau-de-bord.jpg" width="260" alt="Tableau de bord Radthal"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-ckpool.jpg" width="260" alt="Page ckpool"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-noeud.jpg" width="260" alt="Page du nœud"> |

*(Captures d'un nœud en marche : solde masqué, adresse du réseau local remplacée.)*

## Ajouter la boutique à Umbrel

1. Ouvre l'**App Store** d'Umbrel.
2. Clique sur les **trois points** en haut à droite, puis sur **Community App Stores**.
3. Colle l'adresse `https://github.com/bryancarterdouglas/Umbrel-radthal` et confirme.

Les boutiques communautaires ne sont pas vérifiées par l'équipe d'Umbrel : n'en ajoute que de développeurs en qui tu as confiance, et lis le code (il est court, dans les dossiers `radthal-*`).

## Ce que les applications supposent

Les pages parlent au **RPC du nœud** et lisent quelques fichiers du Pi. Sur le Pi de l'auteur :

| Élément | Emplacement / valeur |
|---|---|
| Nœud Radthal avec son RPC | sur la machine hôte, port **9332**, utilisateur `radthal` |
| Mot de passe du RPC | fichier `/home/umbrel/radthal/rpcpass.txt` (monté en lecture seule) |
| Programmes du nœud (pour **Radthal Node**) | compilés dans `/home/umbrel/radthal/build`, données dans `/home/umbrel/radthal-data` |
| Image du nœud (pour **Radthal Node**) | `localhost:5000/radthal-build:v1`, servie par un petit registre local sur le Pi (Umbrel télécharge toujours les images) |
| Journaux de ckpool (pour **ckpool** et **Dashboard**) | `/home/umbrel/radthal/ckpool/logs` |
| Portefeuille du nœud (pour **Wallet**) | nommé `minage` |
| Dossier des certificats (pour **Horodatage**) | `/home/umbrel/radthal/horodatage` |

Si ta disposition est différente, il faut adapter ces chemins dans les fichiers `docker-compose.yml` de chaque application.

## Sécurité

- Les pages sont en **HTTP, sur le réseau local** : ne les expose **jamais** sur Internet.
- N'ouvre jamais le RPC du nœud (ni le port Stratum de ckpool) à Internet. Pour aider le réseau, seul le port **9333** (P2P) se redirige, et seulement pour le nœud.
- Ne montre jamais l'adresse de ton portefeuille, son QR code ni ton solde dans des captures publiques.

## Problème ou question

Ouvre une « issue » sur ce dépôt. Radthal est un projet expérimental, sans aucune garantie, sans valeur promise et sans lien avec Bitcoin ni avec ses développeurs.

## English

Six small web apps for [Umbrel](https://umbrel.com) to view and use a [Radthal](https://github.com/bryancarterdouglas/radthal) node (an experimental Bitcoin Core fork): node status, dashboard, mempool, ckpool stats, wallet (with short on-chain messages) and document timestamping. **This is not a one-click install**: the apps were built for the author's Raspberry Pi and read the node's RPC (port 9332) and files at fixed paths (see the table above). To simply run a node, follow the Docker instructions in the main repository. To add the store: App Store → ⋯ (top right) → Community App Stores → paste `https://github.com/bryancarterdouglas/Umbrel-radthal`. Community stores are not vetted by Umbrel. The pages are plain HTTP for the local network: never expose them to the Internet. Experimental software, no warranty, no affiliation with Bitcoin.

## Licence

MIT (voir `LICENSE`).
