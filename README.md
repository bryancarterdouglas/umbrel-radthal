# Umbrel-radthal : Radthal pour Umbrel

🇬🇧 [English version](README.en.md)

Boutique d'applications [Umbrel](https://umbrel.com) pour **[Radthal](https://github.com/bryancarterdouglas/radthal)**, un fork expérimental de Bitcoin Core : voir le dépôt principal pour ce que c'est, ses règles (dont la part de 2 % du créateur à partir du bloc 25 000) et ses avertissements.

## L'application « Radthal » : installation en un clic

Une application **autonome** : un seul bouton « Installer », sans rien compiler ni copier sur l'Umbrel. Elle lance :

- le **nœud** Radthal ;
- le serveur de minage **ckpool** (mode solo), pour y brancher un mineur SHA-256 (Avalon, Antminer, Bitaxe…) ;
- toutes les **pages**, derrière un menu en haut et protégées par le mot de passe d'Umbrel : Nœud, Tableau de bord, Mempool, ckpool, Portefeuille et Horodatage.

Installe **Radthal** (et non « Radthal Node ») depuis cette boutique (voir [Ajouter la boutique à Umbrel](#ajouter-la-boutique-à-umbrel)).

**Miner.** Branche ton mineur sur le port **3333** de l'Umbrel, avec ton adresse `rdt1…` comme nom d'utilisateur : la page **ckpool** donne les réglages. Ce port est fait pour ton réseau local : ne le redirige jamais depuis ta box.

**Ce qui a été testé.** Sur un Raspberry Pi 5 sous Umbrel : installation, synchronisation du nœud, minage avec un Avalon (un bloc trouvé), portefeuille et redémarrage du Pi. Les images existent aussi pour amd64, mais ce n'est pas testé sur Umbrel. Radthal reste un projet expérimental, sans valeur garantie.

**Avant d'installer.** Les ports 9333 (réseau) et 3333 (minage) ne peuvent servir qu'une fois : si tu as déjà un nœud Radthal sur cet Umbrel (les anciennes applications séparées « Radthal Node », « Radthal Wallet »…), ou un `ckpool` lancé à la main, arrête-les d'abord.

### Reprendre un portefeuille existant

L'application crée son propre portefeuille, `monportefeuille`, vide au départ. Pour reprendre celui d'un ancien nœud (un dossier qui contient `wallet.dat`) : **sauvegarde-le**, arrête l'ancien nœud, puis copie son dossier dans celui de l'application, sous ce nom :

```bash
sudo mkdir -p ~/umbrel/app-data/radthal-one/data/wallets
sudo cp -a /chemin/de/ton/ancien/portefeuille ~/umbrel/app-data/radthal-one/data/wallets/monportefeuille
sudo chown -R 1000:1000 ~/umbrel/app-data/radthal-one/data/wallets
```

Ouvre ensuite la page **Portefeuille** et clique sur « Créer mon portefeuille » : le portefeuille existant est rechargé. Ne fais pas tourner l'ancien nœud avec le même portefeuille en même temps : deux copies actives finiraient par se contredire.

## Les pages de l'application

- **Nœud** : état du nœud (blocs, synchronisation, pairs avec un pseudo, jamais leur adresse, ports).
- **Tableau de bord** : vue d'ensemble : derniers blocs, surveillance des forks, difficulté, puissance du réseau, mineur.
- **Mempool** : transactions en attente, répartition des frais, derniers blocs.
- **ckpool** : statistiques du pool solo : mineurs, hashrate, parts, réglages de connexion d'un mineur.
- **Portefeuille** : solde, adresse de réception avec QR code, envoi de RDT (aperçu puis confirmation), historique, sauvegarde, messages courts écrits dans la chaîne.
- **Horodatage** : prouver qu'un document existait à la date d'un bloc (l'empreinte SHA-256 est calculée dans le navigateur ; le document ne quitte jamais ton appareil).

Les quatre premières pages **regardent** seulement. Portefeuille et Horodatage **agissent** : les clés restent dans le nœud, les pages ne les voient jamais. Un message écrit dans la chaîne est **public et définitif**.

| Tableau de bord | ckpool | Nœud |
|:---:|:---:|:---:|
| <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-tableau-de-bord.jpg" width="260" alt="Tableau de bord Radthal"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-ckpool.jpg" width="260" alt="Page ckpool"> | <img src="https://raw.githubusercontent.com/bryancarterdouglas/radthal/main/docs/interface-noeud.jpg" width="260" alt="Page du nœud"> |

*(Captures d'un nœud en marche : solde masqué, adresse du réseau local remplacée.)*

## Ajouter la boutique à Umbrel

1. Ouvre l'**App Store** d'Umbrel.
2. Clique sur les **trois points** en haut à droite, puis sur **Community App Stores**.
3. Colle l'adresse `https://github.com/bryancarterdouglas/umbrel-radthal` et confirme.

Les boutiques communautaires ne sont pas vérifiées par l'équipe d'Umbrel : n'en ajoute que de développeurs en qui tu as confiance, et lis le code (il est court, dans le dossier `radthal-one`).

## Sécurité

- Les pages sont en **HTTP, sur le réseau local** : ne les expose **jamais** sur Internet.
- N'ouvre jamais le RPC du nœud (ni le port Stratum de ckpool) à Internet. Pour aider le réseau, seul le port **9333** (P2P) se redirige, et seulement pour le nœud.
- Ne montre jamais l'adresse de ton portefeuille, son QR code ni ton solde dans des captures publiques.

## Problème ou question

Ouvre une « issue » sur ce dépôt. Radthal est un projet expérimental, sans aucune garantie, sans valeur promise et sans lien avec Bitcoin ni avec ses développeurs.

## Licence

MIT (voir `LICENSE`).
