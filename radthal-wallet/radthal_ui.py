"""Interface web des applications Umbrel Radthal (noeud, tableau de bord, mempool, ckpool, portefeuille).

Un seul fichier, aucune dependance (Python 3 seulement). Le serveur lit le noeud (RPC) et les
fichiers de ckpool, et sert :
  /           la page (HTML + CSS + JS, style anime comme la video de presentation)
  /api/state  les donnees de la vue choisie par la variable VIEW, en JSON
  /api/preview, /api/send, /api/newaddress, /api/encrypt, /api/backup, /api/msgpreview, /api/msgsend
  (vue "wallet" seulement, requetes POST en JSON)

La page interroge /api/state toutes les 3 secondes et met l'affichage a jour sans recharger.
"""
import base64, glob, json, math, os, re, secrets, threading, time, unicodedata, urllib.error, urllib.parse, urllib.request
from decimal import Decimal, ROUND_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIEW = os.environ.get("VIEW", "dashboard")
RPC = os.environ.get("RPC", "http://radthal-host:9332")
RPCUSER = os.environ.get("RPCUSER", "radthal")
PASSFILE = os.environ.get("PASSFILE", "/run/secrets/radthal_rpc_password")
LOGS = os.environ.get("LOGS", "/ckpool/logs")
PORT = int(os.environ.get("PORT", "8000"))
WALLET = os.environ.get("WALLET", "minage")
STRATUM = os.environ.get("STRATUM_PORT", "3333")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data")                 # dossier de sauvegarde, vu par le noeud
BACKUP_SHOW = os.environ.get("BACKUP_SHOW", BACKUP_DIR)            # le meme dossier, tel qu'on le voit sur le Pi
TITLES = {"node": "Nœud Radthal", "dashboard": "Tableau de bord Radthal",
          "mempool": "Mempool Radthal", "ckpool": "ckpool Radthal", "wallet": "Portefeuille Radthal"}
SUBTITLES = {"node": "L'état de ton nœud complet",
             "dashboard": "La chaîne Radthal en direct",
             "mempool": "Les transactions en attente d'un bloc",
             "ckpool": "Ton mineur et ton pool solo",
             "wallet": "Reçois et envoie tes RDT depuis ton nœud"}


# ---------------------------------------------------------------- acces au noeud

def rpc(method, params=None, wallet=None, timeout=3):
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        url = RPC + ("/wallet/" + wallet if wallet else "")
        body = json.dumps({"jsonrpc": "1.0", "id": "u", "method": method, "params": params or []}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "Authorization": auth})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["result"]
    except Exception:
        return None


_BLK = {}          # hash -> bloc (immuable, donc mis en cache)
_BLK_LOCK = threading.Lock()


def get_block(bh):
    with _BLK_LOCK:
        b = _BLK.get(bh)
    if b is not None:
        return b
    r = rpc("getblock", [bh])
    if not r:
        return None
    b = {"h": r.get("height"), "hash": r.get("hash"), "time": r.get("time"), "ntx": r.get("nTx"),
         "size": r.get("size"), "prev": r.get("previousblockhash")}
    with _BLK_LOCK:
        _BLK[bh] = b
        while len(_BLK) > 600:
            _BLK.pop(next(iter(_BLK)))
    return b


def recent_blocks(n=14):
    """Les n derniers blocs de la meilleure chaine, du plus ancien au plus recent."""
    out, bh = [], rpc("getbestblockhash")
    while bh and len(out) < n:
        b = get_block(bh)
        if not b:
            break
        out.append(b)
        bh = b["prev"]
    out.reverse()
    return out


_W = {"mined": {}, "light": False, "scanned": False}
_W_LOCK = threading.Lock()
FULL_SCAN = VIEW in ("dashboard", "ckpool")     # seules ces pages affichent le total de blocs trouves


def _slim(t):
    return {"blockhash": t.get("blockhash"), "amount": t.get("amount"), "confirmations": t.get("confirmations"),
            "blockheight": t.get("blockheight")}


def _wallet_apply(txs, mined):
    for t in txs:
        bh = t.get("blockhash")
        if not bh:
            continue
        if t.get("category") in ("generate", "immature"):
            mined[bh] = _slim(t)
        elif t.get("category") == "orphan":
            mined.pop(bh, None)


def wallet_refresh():
    """1) les 200 dernieres transactions (rapide) : sert a marquer tes blocs ;
    2) une seule fois, tout le portefeuille en UN appel (plusieurs appels par pages seraient bien plus lourds)."""
    txs = rpc("listtransactions", ["*", 200, 0], wallet=WALLET)
    if txs is None:
        return
    with _W_LOCK:
        _wallet_apply(txs, _W["mined"])
        _W["light"] = True
        if not FULL_SCAN:
            _W["scanned"] = True
    if FULL_SCAN and not _W["scanned"]:
        allt = rpc("listtransactions", ["*", 10000000, 0], wallet=WALLET, timeout=120)
        if allt is None:
            return
        mined = {}
        _wallet_apply(allt, mined)
        del allt
        with _W_LOCK:
            mined.update(_W["mined"])        # les infos les plus fraiches gagnent
            _W["mined"], _W["scanned"] = mined, True


def wallet_loop():
    while True:
        try:
            wallet_refresh()
        except Exception:
            pass
        time.sleep(8 if _W["light"] else 4)


def wallet_blocks():
    """Blocs trouves par ce portefeuille : {hash du bloc: infos de la transaction coinbase}."""
    with _W_LOCK:
        return dict(_W["mined"])


def found_card(mine, sub="par ce portefeuille"):
    if not _W["scanned"]:
        return card("found", "Blocs trouvés", value="calcul…", sub="lecture du portefeuille (une fois, ça peut prendre une minute)")
    return card("found", "Blocs trouvés", num=len(mine), sub=sub, accent=len(mine) > 0)


# ---------------------------------------------------------------- formats

SUF = {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "Z": 1e21}


def parse_hs(v):
    """'6.71T' (format de ckpool) -> 6.71e12"""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"^\s*([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*([KMGTPEZ]?)", str(v or ""))
    return float(m.group(1)) * SUF[m.group(2)] if m else 0.0


def hs_parts(x):
    x = float(x or 0)
    for u in ["H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s"]:
        if x < 1000:
            return round(x, 2), u
        x /= 1000
    return round(x, 2), "EH/s"


def scale(n, units=("", "k", "M", "G", "T", "P")):
    """Nombre -> (valeur, decimales, unite) pour un affichage court."""
    n = float(n or 0)
    for u in units[:-1]:
        if abs(n) < 1000:
            return (round(n, 2), 0 if n == int(n) or abs(n) >= 100 else 2, u)
        n /= 1000
    return round(n, 2), 2, units[-1]


def size_parts(n):
    n = float(n or 0)
    for u in ["o", "Ko", "Mo", "Go"]:
        if n < 1024:
            return round(n, 1), u
        n /= 1024
    return round(n, 1), "To"


def sig(n, digits=3):
    if n <= 0:
        return 0
    k = digits - int(math.floor(math.log10(n))) - 1
    return round(n, k) if k > 0 else int(round(n, k))


def fnum(n, d=2):
    return ("%.*f" % (d, float(n))).replace(".", ",")


def fhs(x):
    n, u = hs_parts(x)
    return "%s %s" % (fnum(n, 2), u)


def fsc(x):
    n, d, u = scale(x)
    return ("%s %s" % (fnum(n, d), u)).strip()


def fsize(x):
    n, u = size_parts(x)
    return "%s %s" % (fnum(n, 1), u)


def plural(n, word):
    try:
        n = int(n)
    except Exception:
        return "-"
    return "%d %s%s" % (n, word, "s" if n > 1 else "")


def auto_dec(x, maxd=8):
    """Nombre de decimales utiles (sans zeros inutiles a la fin)."""
    t = ("%.*f" % (maxd, float(x))).rstrip("0")
    return len(t.split(".")[1]) if "." in t else 0


def fr_int(n):
    return "{:,}".format(int(round(n))).replace(",", " ")


def dur(s):
    s = int(s)
    if s < 90:
        return "%d s" % s
    if s < 5400:
        return "%d min" % round(s / 60)
    if s < 172800:
        h, m = divmod(s // 60, 60)
        return "%d h %02d" % (h, m)
    return "%d j %d h" % (s // 86400, s % 86400 // 3600)


def tries_text(h):
    """Une phrase simple : combien d'essais par seconde fait le mineur."""
    if h <= 0:
        return ""
    if h >= 1e12:
        return "≈ %s milliards d'essais par seconde" % fr_int(sig(h / 1e9))
    if h >= 1e9:
        return "≈ %s millions d'essais par seconde" % fr_int(sig(h / 1e6))
    if h >= 1e6:
        return "≈ %s milliers d'essais par seconde" % fr_int(sig(h / 1e3))
    return "≈ %s essais par seconde" % fr_int(sig(h))


def short_addr(s):
    s = str(s)
    return s if len(s) <= 18 else s[:8] + "…" + s[-4:]


def card(i, label, num=None, dec=0, unit="", value=None, sub="", tone=None, accent=False):
    return {"id": i, "label": label, "num": num, "dec": dec, "unit": unit, "value": value,
            "sub": sub, "tone": tone, "accent": accent}


def cards(i, items, title=None):
    return {"id": i, "type": "cards", "title": title, "items": items}


def table(i, title, head, rows, empty="Rien à afficher."):
    return {"id": i, "type": "table", "title": title, "head": head, "rows": rows, "empty": empty}


def kv(i, title, rows):
    return {"id": i, "type": "kv", "title": title, "rows": rows}


def note(i, text, tone=None):
    return {"id": i, "type": "note", "text": text, "tone": tone}


# ---------------------------------------------------------------- ckpool

def ckpool_status():
    out = {}
    try:
        for line in open(LOGS + "/pool/pool.status"):
            line = line.strip()
            if line:
                out.update(json.loads(line))
    except Exception:
        pass
    return out


def ckpool_workers():
    """Liste des appareils connectes au pool, tous utilisateurs confondus."""
    res = []
    for f in sorted(glob.glob(LOGS + "/users/*")):
        try:
            u = json.load(open(f))
        except Exception:
            continue
        ws = u.get("worker")
        for w in ws if isinstance(ws, list) else []:
            if isinstance(w, dict):
                res.append(w)
        if not isinstance(ws, list) or not ws:
            res.append({"workername": os.path.basename(f), **{k: v for k, v in u.items() if not isinstance(v, (list, dict))}})
    return res


def worker_label(name):
    name = str(name)
    if "." in name:
        return name.split(".", 1)[1] or "(sans nom)"
    return "(sans nom)"


def blocks_section(h, mine, i="chain", title="Les derniers blocs"):
    blocks = recent_blocks() if h is not None else []
    return {"id": i, "type": "chain", "title": title, "blocks": [
        {"h": b["h"], "hash": b["hash"], "time": b["time"], "ntx": b["ntx"], "size": b["size"],
         "mine": b["hash"] in mine} for b in blocks]}


def miner_section(st, workers, nethash):
    now = time.time()
    h1 = parse_hs(st.get("hashrate1m"))
    last = max([float(w.get("lastshare") or 0) for w in workers] or [0])
    online = int(st.get("Workers") or 0)
    if not st and not workers:
        state, label = "off", "Pas de données ckpool"
    elif online > 0 and now - last < 180:
        state, label = "on", "En train de miner"
    else:
        state, label = "idle", "En attente de ton mineur"
    num, unit = hs_parts(h1)
    return {"id": "miner", "type": "miner", "state": state, "label": label, "hashrate": {"num": num, "unit": unit},
            "tries": tries_text(h1), "workers": online, "lastshare": last or None}


# ---------------------------------------------------------------- vues

def common_cards(info, nh, mine_txs):
    h = info.get("blocks")
    diff = info.get("difficulty")
    left = None if h is None else 2016 - (h + 1) % 2016
    n, u = hs_parts(nh)
    if not info:
        return [card(i, l, value="-") for i, l in (("height", "Blocs"), ("diff", "Difficulté"), ("nethash", "Puissance du réseau"), ("left", "Blocs avant l'ajustement"))]
    return [
        card("height", "Blocs", num=h, accent=True) if h is not None else card("height", "Blocs", value="-", accent=True),
        card("diff", "Difficulté", num=round(diff, 2 if diff < 1000 else 0), dec=2 if diff < 1000 else 0) if diff is not None else card("diff", "Difficulté", value="-"),
        card("nethash", "Puissance du réseau", num=n, dec=2, unit=u, sub="estimée sur les 30 derniers blocs"),
        card("left", "Blocs avant l'ajustement", num=left, sub="La difficulté s'ajuste tous les 2 016 blocs") if left is not None else card("left", "Blocs avant l'ajustement", value="-"),
    ]


def view_dashboard():
    info = rpc("getblockchaininfo") or {}
    secs = []
    st = ckpool_status()
    workers = ckpool_workers()
    if not info:
        secs.append(note("down", "Le nœud ne répond pas. Vérifie qu'il est démarré dans Umbrel.", "bad"))
        secs.append(miner_section(st, workers, 0))
        return secs, info
    mine = wallet_blocks()
    mp = rpc("getmempoolinfo") or {}
    net = rpc("getnetworkinfo") or {}
    bal = (rpc("getbalances", wallet=WALLET) or {}).get("mine", {})
    nh = rpc("getnetworkhashps", [30]) or 0
    secs.append(blocks_section(info.get("blocks"), mine))
    items = common_cards(info, nh, mine)
    items += [
        card("peers", "Pairs connectés", num=net.get("connections")) if net else card("peers", "Pairs connectés", value="-"),
        card("mempool", "Transactions en attente", num=mp.get("size")) if mp else card("mempool", "Transactions en attente", value="-"),
        found_card(mine),
        card("bal", "Solde disponible", num=round(bal.get("trusted") or 0, 2), dec=2 if (bal.get("trusted") or 0) < 10000 else 0, unit="RDT") if bal else card("bal", "Solde disponible", value="-"),
        card("imm", "Solde en maturation", num=round(bal.get("immature") or 0, 2), dec=2 if (bal.get("immature") or 0) < 10000 else 0, unit="RDT",
             sub="Une récompense devient utilisable après 100 blocs") if bal else card("imm", "Solde en maturation", value="-"),
    ]
    secs.append(cards("stats", items))
    secs.append(miner_section(st, workers, nh))
    return secs, info


def view_node():
    info = rpc("getblockchaininfo") or {}
    net = rpc("getnetworkinfo") or {}
    peers = rpc("getpeerinfo") or []
    up = rpc("uptime")
    secs = []
    if not info:
        secs.append(note("down", "Le nœud ne répond pas. Vérifie qu'il est démarré dans Umbrel.", "bad"))
        return secs, info
    warn = info.get("warnings")
    if isinstance(warn, list):
        warn = " ; ".join(warn)
    blocks, headers = info.get("blocks", 0), info.get("headers", 0)
    tip_t = info.get("time") or info.get("mediantime")
    recent = bool(tip_t) and time.time() - tip_t < 3600
    synced = headers > 0 and blocks >= headers and (not info.get("initialblockdownload") or recent)
    pct = 100.0 if synced else (blocks / headers * 100 if headers else 0.0)
    secs.append(blocks_section(blocks, wallet_blocks()))
    dn, du = size_parts(info.get("size_on_disk"))
    inb = sum(1 for p in peers if p.get("inbound"))
    secs.append(cards("stats", [
        card("state", "État", value="En marche", tone="good"),
        card("height", "Blocs", num=blocks, accent=True),
        card("sync", "Synchronisation", num=round(min(pct, 100), 1), dec=1, unit="%",
             sub="À jour" if synced else "Rattrapage en cours (%s / %s)" % (blocks, headers), tone="good" if synced else "warn"),
        card("diff", "Difficulté", num=round(info.get("difficulty") or 0, 2), dec=2),
        card("peers", "Connexions", num=net.get("connections"), sub="%d entrantes · %d sortantes" % (inb, len(peers) - inb)),
        card("disk", "Taille sur disque", num=dn, dec=1, unit=du),
        card("ver", "Version", value=str(net.get("subversion", "-")).strip("/")),
        card("up", "En marche depuis", value=dur(up) if up else "-"),
    ]))
    if warn:
        secs.append(note("warn", "Avertissement du nœud : " + str(warn), "warn"))
    secs.append(table("peers", "Pairs connectés", ["Adresse", "Version", "Sens", "Reçu", "Envoyé", "Connecté depuis"], [
        [p.get("addr"), str(p.get("subver", "")).strip("/"), "entrant" if p.get("inbound") else "sortant",
         fsize(p.get("bytesrecv")), fsize(p.get("bytessent")), {"age": p.get("conntime")}]
        for p in peers], "Aucun pair connecté pour l'instant."))
    secs.append(kv("ports", "Ports", [["P2P (réseau)", "9333"], ["RPC (local)", "9332"], ["Stratum (mineurs)", STRATUM]]))
    return secs, info


def view_mempool():
    inf = rpc("getblockchaininfo") or {}
    secs = []
    if not inf:
        secs.append(note("down", "Le nœud ne répond pas. Vérifie qu'il est démarré dans Umbrel.", "bad"))
        return secs, inf
    info = rpc("getrawmempool", [True])
    mp = rpc("getmempoolinfo") or {}
    txs = []
    for txid, t in (info or {}).items():
        vs = t.get("vsize") or 0
        fee = (t.get("fees") or {}).get("base", 0)
        rate = fee * 1e8 / vs if vs else 0
        txs.append((rate, txid, vs, fee, t.get("time")))
    txs.sort(reverse=True)
    secs.append(blocks_section(inf.get("blocks"), wallet_blocks()))
    bn, bu = size_parts(mp.get("bytes"))
    secs.append(cards("stats", [
        card("size", "Transactions en attente", num=mp.get("size"), accent=True) if mp else card("size", "Transactions en attente", value="-"),
        card("bytes", "Taille", num=bn, dec=0 if bu == "o" else 1, unit=bu),
        card("fees", "Frais totaux", num=round(mp.get("total_fee") or 0, 8), dec=auto_dec(mp.get("total_fee") or 0), unit="RDT") if mp else card("fees", "Frais totaux", value="-"),
        card("minfee", "Frais minimum", num=mp.get("mempoolminfee"), dec=auto_dec(mp.get("mempoolminfee") or 0), unit="RDT/kvB") if mp.get("mempoolminfee") is not None else card("minfee", "Frais minimum", value="-"),
    ]))
    if not txs:
        secs.append(note("empty", "Aucune transaction en attente. Elles sont confirmées très vite tant que les blocs sont rapides."))
    else:
        edges = [(0, 2, "< 2"), (2, 5, "2 - 5"), (5, 10, "5 - 10"), (10, 20, "10 - 20"), (20, 50, "20 - 50"), (50, 1e18, "50 +")]
        bars = []
        for lo, hi, lab in edges:
            sel = [t for t in txs if lo <= t[0] < hi]
            bars.append({"label": lab + " sat/vB", "count": len(sel), "size": sum(t[2] for t in sel)})
        secs.append({"id": "bars", "type": "bars", "title": "Répartition des frais", "items": bars})
        secs.append(table("txs", "Transactions (les 50 premières, par frais décroissants)",
                          ["Txid", "sat/vB", "Taille vB", "Attente"],
                          [[t[1][:12] + "…", fnum(t[0], 1), t[2], {"age": t[4]}] for t in txs[:50]]))
    return secs, inf


def view_ckpool():
    st = ckpool_status()
    workers = ckpool_workers()
    info = rpc("getblockchaininfo") or {}
    mine = wallet_blocks() if info else {}
    diff = info.get("difficulty") or 0
    secs = []
    miner = miner_section(st, workers, 0)
    secs.append(miner)

    h1, h5, h1h = parse_hs(st.get("hashrate1m")), parse_hs(st.get("hashrate5m")), parse_hs(st.get("hashrate1hr"))
    items = []
    for i, lab, v in (("h1", "Hashrate · 1 min", h1), ("h5", "Hashrate · 5 min", h5), ("h60", "Hashrate · 1 h", h1h)):
        n, u = hs_parts(v)
        items.append(card(i, lab, num=n, dec=2, unit=u, accent=(i == "h1")))
    ws = int(st.get("Workers") or 0)
    items.append(card("workers", "Mineurs connectés", num=ws, sub=plural(st.get("Users"), "utilisateur") if st.get("Users") is not None else "",
                      tone="good" if ws else "warn"))
    acc, rej = float(st.get("accepted") or 0), float(st.get("rejected") or 0)
    n, d, u = scale(acc)
    items.append(card("acc", "Parts acceptées", num=n, dec=d, unit=u, sub="Chaque part est un essai valide envoyé au pool"))
    rate = (rej / (acc + rej) * 100) if acc + rej else 0
    items.append(card("rej", "Parts rejetées", num=round(rate, 2), dec=2, unit="%",
                      sub="Doit rester proche de 0 %", tone=("good" if rate < 2 else "warn") if acc + rej else None))
    best = max([float(w.get("bestever") or 0) for w in workers] + [float(st.get("bestshare") or 0)])
    if best:
        n, d, u = scale(best)
        if not diff:
            sub = ""
        elif best >= diff:
            sub = "Au-dessus de la difficulté du réseau : de quoi gagner un bloc"
        else:
            sub = "%s %% du chemin vers un bloc" % fnum(best / diff * 100, 4)
        items.append(card("best", "Meilleure part", num=n, dec=d, unit=u, sub=sub))
    tip = info.get("blocks")
    if info and _W["light"] and tip is not None and any(t.get("blockheight") is not None for t in mine.values()):
        last100 = sum(1 for t in mine.values() if (t.get("blockheight") or -1) > tip - 100)
        items.append(card("last100", "Tes blocs · 100 derniers", num=last100, unit="/ 100",
                          sub="Part des derniers blocs trouvés par ce mineur", accent=last100 > 0))
    if diff > 0 and h5 > 0:
        exp = diff * 4294967296.0 / h5
        items.append(card("eta", "Un bloc en moyenne toutes les", value="≈ " + dur(exp),
                          sub="Une moyenne, pas une promesse : c'est la chance qui décide"))
    if info:
        items.append(found_card(mine))
    if not st:      # ckpool n'a rien ecrit : inutile d'afficher des compteurs a zero
        items = [it for it in items if it["id"] == "found"]
    if items:
        secs.append(cards("stats", items))

    now = time.time()
    rows = []
    for w in workers:
        ls = float(w.get("lastshare") or 0)
        if not ls or now - ls > 86400:
            continue          # ancien appareil, inactif depuis plus d'un jour
        active = ls and now - ls < 180
        rows.append([{"t": "● en ligne" if active else "○ inactif", "cls": "good" if active else "muted"},
                     worker_label(w.get("workername")), fhs(parse_hs(w.get("hashrate1m"))),
                     fhs(parse_hs(w.get("hashrate5m"))),
                     {"age": ls} if ls else "-",
                     fsc(w.get("shares") or 0),
                     fsc(w.get("bestever") or 0)])
    secs.append(table("workers", "Tes appareils", ["État", "Nom", "Hashrate 1 min", "Hashrate 5 min", "Dernière part", "Parts", "Meilleure part"], rows,
                      "Aucun mineur actif ces dernières 24 h. Branche ton ASIC avec les réglages ci-dessous."))

    secs.append({"id": "connect", "type": "connect", "title": "Connecter un mineur", "items": [
        {"label": "URL du pool", "value": "stratum+tcp://{host}:" + STRATUM, "copy": True},
        {"label": "Utilisateur", "value": "ton adresse rdt1… (ajoute .nom pour nommer l'appareil)", "copy": False},
        {"label": "Mot de passe", "value": "x", "copy": True}],
        "warn": "Le port %s sert uniquement à ton réseau local. Ne l'ouvre jamais sur Internet." % STRATUM})

    if mine:
        top = sorted(mine.values(), key=lambda t: t.get("confirmations") or 0)[:8]
        rows = []
        for t in top:
            b = get_block(t["blockhash"])
            conf = int(t.get("confirmations") or 0)
            rows.append([b["h"] if b else "-", {"age": b["time"]} if b else "-", fnum(abs(float(t.get("amount") or 0)), 2) + " RDT",
                         {"t": "utilisable", "cls": "good"} if conf >= 101 else {"t": "%d / 100" % max(conf - 1, 0), "cls": "warn"}])
        secs.append(table("mine", "Derniers blocs trouvés par ce mineur", ["Bloc", "Il y a", "Récompense", "Maturité"], rows))
    if info:
        secs.append(blocks_section(info.get("blocks"), mine))
    return secs, info


# ---------------------------------------------------------------- portefeuille (VIEW=wallet)
# Cette page parle au portefeuille du noeud (RPC). Les cles restent dans le noeud : la page ne les voit jamais.
# Envoyer des RDT se fait en deux temps : un apercu (rien n'est envoye), puis une confirmation qui doit
# reprendre le jeton de l'apercu (a usage unique, valable 5 minutes). Si le portefeuille est chiffre,
# le mot de passe est demande a chaque envoi, sert a deverrouiller le portefeuille 30 secondes au plus,
# puis le portefeuille est reverrouille tout de suite. Le mot de passe n'est ni garde ni ecrit nulle part.

RECV_LABEL = "reception-courante"
OLD_LABEL = "reception"
SAT = Decimal("0.00000001")
_WLOCK = threading.Lock()      # un seul envoi / changement d'adresse a la fois
_PREV = {}                     # jeton d'apercu -> {addr, amount, all, rate, t}
_FAILS = []                    # instants des mots de passe refuses (limite : 5 en 10 minutes)
_USED = {"addr": None, "t": 0.0, "v": False}


class UserError(Exception):
    """Message destine a la personne qui utilise la page."""


def rpc_full(method, params=None, wallet=None, timeout=20):
    """Appel RPC qui renvoie (resultat, erreur). Les montants restent exacts (Decimal).
    params peut etre une liste ou un dictionnaire (parametres nommes)."""
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        url = RPC + ("/wallet/" + urllib.parse.quote(wallet, safe="") if wallet else "")
        body = json.dumps({"jsonrpc": "1.0", "id": "w", "method": method,
                           "params": [] if params is None else params}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "Authorization": auth})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r, parse_float=Decimal)
        except urllib.error.HTTPError as e:
            try:
                d = json.load(e, parse_float=Decimal)
            except Exception:
                return None, {"code": -1, "message": "Erreur HTTP %s du nœud" % e.code}
        if d.get("error"):
            return None, d["error"]
        return d.get("result"), None
    except Exception:
        return None, {"code": -999, "message": "Le nœud ne répond pas"}


def human_error(err):
    code = err.get("code")
    msg = str(err.get("message", ""))
    low = msg.lower()
    if code == -999:
        return "Le nœud ne répond pas. Vérifie qu'il est démarré dans Umbrel."
    if code == -14:
        return "Mot de passe du portefeuille incorrect."
    if code == -15:
        return "Le portefeuille n'est pas chiffré : aucun mot de passe n'est nécessaire."
    if code == -18 or "not loaded" in low or "does not exist" in low and "wallet" in low:
        return "Le portefeuille « %s » n'est pas chargé sur le nœud." % WALLET
    if code == -5:
        return "Cette adresse n'est pas une adresse Radthal valide."
    if code == -6 or "insufficient funds" in low or "exceeds your balance" in low:
        return ("Solde disponible insuffisant. Les récompenses de minage ne sont utilisables "
                "qu'après 100 blocs.")
    if "too large" in low:
        return ("La transaction serait trop volumineuse (trop de petites pièces à regrouper). "
                "Envoie un montant plus petit.")
    if "too small" in low:
        return "Montant trop petit : le réseau refuse les montants minuscules."
    if "too low to pay" in low or "too low" in low and "fee" in low:
        return "Le solde est trop faible pour payer les frais de cet envoi."
    if "fee estimation failed" in low:
        return "Le nœud n'a pas pu estimer les frais."
    if "scriptpubkey" in low or "datacarrier" in low or "nulldata" in low or "op_return" in low:
        return ("Ton nœud refuse cette donnée (OP_RETURN) : soit les transactions de données sont désactivées, soit "
                "ce message dépasse la taille permise par ses réglages (options datacarrier et datacarriersize).")
    return "Refusé par le nœud : " + msg


def parse_amount(txt):
    t = str(txt or "").strip().replace(" ", "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    if not re.fullmatch(r"\d{1,10}(\.\d{1,8})?", t):
        raise UserError("Montant invalide : écris un nombre positif avec au plus 8 décimales (par exemple 12,5).")
    d = Decimal(t)
    if d <= 0:
        raise UserError("Le montant doit être supérieur à zéro.")
    return d


def plain(d):
    """Montant en texte pour le noeud : 12.5 (point, jamais d'exposant)."""
    s = format(Decimal(d).quantize(SAT), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_rdt(d):
    """Montant pour l'affichage : 1 234,5 (espaces, virgule)."""
    ip, _, fp = plain(d).partition(".")
    return "{:,}".format(int(ip)).replace(",", " ") + ("," + fp if fp else "")


def fee_rate():
    """Frais en satoshis par octet virtuel : le minimum accepte par le noeud (au moins 1)."""
    rate = Decimal(1)
    mp, e = rpc_full("getmempoolinfo")
    if mp:
        for k in ("mempoolminfee", "minrelaytxfee"):
            v = mp.get(k)
            if v is not None:
                rate = max(rate, (Decimal(v) * 100000).quantize(Decimal("0.01"), rounding=ROUND_UP))
    return rate


def wallet_balances():
    r, e = rpc_full("getbalances", wallet=WALLET)
    return (r or {}).get("mine") if not e else None


def current_receive(create=True):
    """L'adresse de reception affichee : la seule qui porte l'etiquette RECV_LABEL."""
    res, e = rpc_full("getaddressesbylabel", [RECV_LABEL], wallet=WALLET)
    if res:
        return sorted(res.keys())[0]
    if not create:
        return None
    with _WLOCK:
        res, e = rpc_full("getaddressesbylabel", [RECV_LABEL], wallet=WALLET)
        if res:
            return sorted(res.keys())[0]
        addr, e = rpc_full("getnewaddress", [RECV_LABEL], wallet=WALLET)
        return addr if not e else None


def wallet_new_address():
    with _WLOCK:
        res, e = rpc_full("getaddressesbylabel", [RECV_LABEL], wallet=WALLET)
        for a in (res or {}):
            rpc_full("setlabel", [a, OLD_LABEL], wallet=WALLET)
        addr, e = rpc_full("getnewaddress", [RECV_LABEL], wallet=WALLET)
        if e:
            raise UserError(human_error(e))
    _cache["t"] = 0
    return addr


TX_NAMES = {"send": "Envoyé", "receive": "Reçu", "generate": "Minage", "immature": "Minage (en maturation)",
            "orphan": "Bloc orphelin"}


def tx_row(t):
    cat = t.get("category")
    amt = Decimal(t.get("amount", 0))
    conf = int(t.get("confirmations", 0))
    if conf < 0:
        state = "rejetée"
    elif cat in ("generate", "immature"):
        state = "mûr" if conf >= 100 else "%d / 100" % conf
    elif conf == 0:
        state = "en attente"
    else:
        state = "%d conf." % conf
    sign = "+" if amt > 0 else "−"
    txid = str(t.get("txid", ""))
    return [{"age": t.get("time") or 1},
            TX_NAMES.get(cat, str(cat)),
            {"t": sign + fmt_rdt(abs(amt)) + " RDT", "cls": "good" if amt > 0 else ""},
            state,
            {"t": short_addr(t.get("address", "")) if t.get("address") else "-", "title": str(t.get("address", ""))},
            {"t": txid[:8] + "…" + txid[-6:] if len(txid) > 16 else txid, "title": txid}]


def view_wallet():
    info = rpc("getblockchaininfo") or {}
    secs = []
    if not info:
        secs.append(note("down", "Le nœud ne répond pas. Vérifie qu'il est démarré dans Umbrel.", "bad"))
        return secs, info
    wi, e = rpc_full("getwalletinfo", wallet=WALLET)
    if e:
        secs.append(note("nowallet", human_error(e), "bad"))
        return secs, info
    blocks, headers = info.get("blocks", 0), info.get("headers", 0)
    tip_t = info.get("time") or info.get("mediantime")
    recent = bool(tip_t) and time.time() - tip_t < 3600
    synced = headers > 0 and blocks >= headers and (not info.get("initialblockdownload") or recent)
    bal = wallet_balances() or {}
    trusted = Decimal(bal.get("trusted", 0))
    immature = Decimal(bal.get("immature", 0))
    pending = Decimal(bal.get("untrusted_pending", 0))
    encrypted = "unlocked_until" in wi

    def bcard(i, label, v, sub=None, accent=False):
        return card(i, label, num=float(v), dec=max(2, min(8, auto_dec(v))), unit="RDT", sub=sub, accent=accent)

    secs.append(cards("bal", [
        bcard("trusted", "Solde disponible", trusted, accent=True),
        bcard("immature", "En maturation", immature, "Une récompense de minage devient utilisable après 100 blocs"),
        bcard("pending", "En attente", pending, "Reçu, pas encore confirmé"),
    ]))
    if not synced:
        secs.append(note("sync", "Le nœud n'est pas encore à jour (%s blocs sur %s) : le solde peut être incomplet "
                                 "et l'envoi est bloqué." % (blocks, headers), "warn"))
    if not encrypted:
        secs.append(note("unenc", "Ton portefeuille n'est pas protégé par un mot de passe : toute personne qui peut "
                                  "ouvrir cette page peut envoyer tes RDT. Active la protection avec le bloc "
                                  "« Protéger mon portefeuille » ci-dessous.", "warn"))
    addr = current_receive()
    used = False
    if addr:
        # getreceivedbyaddress relit tout le portefeuille : sur un portefeuille de centaines de milliers de
        # transactions c'est lourd, donc on ne le redemande qu'une fois par minute (ou si l'adresse change)
        if _USED["addr"] != addr or time.time() - _USED["t"] > 60:
            got, e = rpc_full("getreceivedbyaddress", [addr, 0], wallet=WALLET)
            _USED.update(addr=addr, t=time.time(), v=bool(got) and Decimal(got) > 0)
        used = _USED["v"]
    secs.append({"id": "receive", "type": "receive", "address": addr, "used": used})
    secs.append({"id": "send", "type": "send", "encrypted": encrypted, "can_send": synced,
                 "spendable": fmt_rdt(trusted), "rate": plain(fee_rate())})
    secs.append(dict(msg_view(), id="msgs", type="msgs", encrypted=encrypted, can_send=synced, max=MSG_MAX))
    if not encrypted:
        secs.append({"id": "protect", "type": "protect"})
    txs, e = rpc_full("listtransactions", ["*", 30, 0, True], wallet=WALLET)
    rows = [tx_row(t) for t in reversed(txs or [])]
    secs.append(table("tx", "Dernières opérations", ["Quand", "Type", "Montant", "État", "Adresse", "Transaction"], rows,
                      "Aucune opération pour l'instant."))
    secs.append({"id": "backup", "type": "backup", "dir": BACKUP_SHOW, "encrypted": encrypted})
    return secs, info


def wallet_preview(addr, amount_txt, send_all):
    addr = str(addr or "").strip()
    if not addr:
        raise UserError("Écris l'adresse du destinataire.")
    if len(addr) > 120 or not re.fullmatch(r"[A-Za-z0-9]+", addr):
        raise UserError("Cette adresse n'est pas une adresse Radthal valide. Vérifie-la caractère par caractère.")
    v, e = rpc_full("validateaddress", [addr])
    if e:
        raise UserError(human_error(e))
    if not v.get("isvalid"):
        raise UserError("Cette adresse n'est pas une adresse Radthal valide. Vérifie-la caractère par caractère.")
    info, _ = rpc_full("getblockchaininfo")
    if info and info.get("headers", 0) > info.get("blocks", 0) + 1 and info.get("initialblockdownload"):
        raise UserError("Le nœud n'est pas encore à jour : attends la fin de la synchronisation.")
    res, e = rpc_full("getbalances", wallet=WALLET)
    if e:
        raise UserError(human_error(e))
    trusted = Decimal(res["mine"].get("trusted", 0))
    rate = fee_rate()
    if send_all:
        if trusted <= 0:
            raise UserError("Aucun solde disponible à envoyer.")
        res, e = rpc_full("sendall", {"recipients": [addr], "fee_rate": plain(rate), "options": {"psbt": True}},
                          wallet=WALLET)
        if e:
            raise UserError(human_error(e))
        dec, e = rpc_full("decodepsbt", [res["psbt"]])
        try:
            fee = Decimal(dec["fee"])
            amount = Decimal(dec["tx"]["vout"][0]["value"])
        except Exception:
            raise UserError("Impossible de calculer les frais de cet envoi.")
    else:
        amount = parse_amount(amount_txt)
        if amount > trusted:
            raise UserError("Solde disponible insuffisant : %s RDT." % fmt_rdt(trusted))
        res, e = rpc_full("walletcreatefundedpsbt",
                          [[], [{addr: plain(amount)}], 0, {"fee_rate": plain(rate), "replaceable": False}],
                          wallet=WALLET)
        if e:
            raise UserError(human_error(e))
        fee = Decimal(res["fee"])
    if amount <= 0:
        raise UserError("Le solde est trop faible pour payer les frais.")
    mine, _ = rpc_full("getaddressinfo", [addr], wallet=WALLET)
    now = time.time()
    for k in [k for k, p in _PREV.items() if now - p["t"] > 300]:
        _PREV.pop(k, None)
    token = secrets.token_hex(16)
    _PREV[token] = {"addr": addr, "amount": plain(amount), "all": bool(send_all), "rate": plain(rate), "t": now}
    return {"token": token, "address": addr, "amount": fmt_rdt(amount), "fee": fmt_rdt(fee),
            "total": fmt_rdt(amount + fee), "self": bool(mine and mine.get("ismine"))}


def wallet_backup():
    """Demande au noeud une copie du portefeuille (le fichier est ecrit par le noeud, pas par cette page)."""
    label = re.sub(r"[^A-Za-z0-9_-]", "", WALLET) or "portefeuille"
    name = "sauvegarde-%s-%s.dat" % (label, time.strftime("%Y%m%d-%H%M%S"))
    with _WLOCK:
        _, e = rpc_full("backupwallet", [BACKUP_DIR.rstrip("/") + "/" + name], wallet=WALLET, timeout=60)
    if e:
        if e.get("code") in (-999, -18):
            raise UserError(human_error(e))
        raise UserError("Impossible de créer la sauvegarde : le nœud n'a pas pu écrire dans son dossier de sauvegarde (%s)."
                        % str(e.get("message", "erreur inconnue")).replace("Error: ", ""))
    return name


def wallet_encrypt(p1, p2, ack):
    if not isinstance(p1, str) or not isinstance(p2, str) or len(p1) > 1024:
        raise UserError("Mot de passe invalide.")
    if p1 != p2:
        raise UserError("Les deux mots de passe ne sont pas identiques.")
    if len(p1) < 8:
        raise UserError("Choisis un mot de passe d'au moins 8 caractères.")
    if ack is not True:
        raise UserError("Coche la case pour confirmer que tu as noté ton mot de passe.")
    with _WLOCK:
        wi, e = rpc_full("getwalletinfo", wallet=WALLET)
        if e:
            raise UserError(human_error(e))
        if "unlocked_until" in wi:
            raise UserError("Le portefeuille est déjà protégé par un mot de passe.")
        _, e = rpc_full("encryptwallet", [p1], wallet=WALLET, timeout=120)
        if e:
            raise UserError(human_error(e))
    _cache["t"] = 0
    # la protection cree de NOUVELLES cles : l'ancienne sauvegarde ne couvre pas ce qui sera recu ensuite
    out = {}
    try:
        out["backup"] = wallet_backup()
    except UserError as e:
        out["backup_error"] = str(e)
    return out


def wallet_send(token, passphrase):
    now = time.time()
    token = str(token or "")
    if not isinstance(passphrase, str) or len(passphrase) > 1024:
        raise UserError("Mot de passe invalide.")
    with _WLOCK:
        p = _PREV.get(token)
        if not p or now - p["t"] > 300:
            _PREV.pop(token, None)
            raise UserError("Cet aperçu a expiré ou a déjà servi. Recommence l'envoi depuis le début.")
        wi, e = rpc_full("getwalletinfo", wallet=WALLET)
        if e:
            raise UserError(human_error(e))
        unlocked = False
        if "unlocked_until" in wi:
            recent = [t for t in _FAILS if now - t < 600]
            _FAILS[:] = recent
            if len(recent) >= 5:
                raise UserError("Trop d'essais de mot de passe. Réessaie dans quelques minutes.")
            if not passphrase:
                raise UserError("Écris le mot de passe de ton portefeuille.")
            _, e = rpc_full("walletpassphrase", [passphrase, 30], wallet=WALLET)
            if e:
                if e.get("code") == -14:
                    _FAILS.append(now)
                raise UserError(human_error(e))
            unlocked = True
        _PREV.pop(token, None)                    # a usage unique : consomme juste avant l'envoi
        try:
            if p["all"]:
                res, e = rpc_full("sendall", {"recipients": [p["addr"]], "fee_rate": p["rate"]}, wallet=WALLET)
                txid = (res or {}).get("txid")
            else:
                res, e = rpc_full("sendtoaddress", {"address": p["addr"], "amount": p["amount"], "fee_rate": p["rate"]},
                                  wallet=WALLET)
                txid = res
        finally:
            if unlocked:
                rpc_full("walletlock", wallet=WALLET)
    if e or not txid:
        raise UserError(human_error(e or {"code": 0, "message": "pas d'identifiant de transaction"}))
    _cache["t"] = 0
    return str(txid)


# ---------------------------------------------------------------- messages
# Un message est ecrit dans la chaine par une transaction dont une sortie OP_RETURN porte "RDTM" suivi du texte
# (UTF-8, 280 caracteres au plus). N'importe qui peut ecrire dans la chaine : les messages lus ici viennent de
# n'importe qui. Ils ne sont affiches que comme du texte, apres nettoyage. Le mot de passe du portefeuille est
# demande a chaque message ; il sert 30 secondes au plus et n'est ni garde ni ecrit nulle part.

MSG_MAGIC = b"RDTM"
MSG_MAGIC_HEX = MSG_MAGIC.hex()
MSG_MAX = 280                                              # caracteres
MSG_BACK = int(os.environ.get("MSG_BACK", "3000"))         # nombre de blocs relus au demarrage
MSG_KEEP = 200                                             # messages gardes en memoire
MSG_SHOWN = 30
_PREVM = {}                    # jeton d'apercu -> {text, rate, t}
_M = {"items": [], "scanned": None, "recent": {}, "tip": 0, "from": None, "ready": False}
_M_LOCK = threading.Lock()
_MINE = {}                     # txid -> True/False : la transaction est-elle dans ce portefeuille ?
_MPEND = []                    # messages ecrits depuis cette page, pas encore vus dans un bloc : {txid, text, t}


def clean_msg(s):
    """Texte sur : retours a la ligne normalises, caracteres de controle et caracteres invisibles retires
    (dont ceux qui inversent le sens de lecture), espaces simplifies."""
    s = str(s).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    out = []
    for c in s:
        cat = unicodedata.category(c)
        if c == "\n":
            out.append(c)
        elif cat in ("Zl", "Zp"):
            out.append("\n")
        elif cat in ("Cc", "Cs", "Co", "Cn") or (cat == "Cf" and c != "‍"):
            continue
        else:
            out.append(c)
    s = "".join(out)
    s = re.sub(r"[  ]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def rpc_batch(calls, wallet=None, timeout=60):
    """Plusieurs appels RPC dans UNE requete HTTP. Renvoie la liste des resultats (None = cet appel a echoue),
    ou None si la requete entiere echoue."""
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        url = RPC + ("/wallet/" + urllib.parse.quote(wallet, safe="") if wallet else "")
        body = json.dumps([{"jsonrpc": "1.0", "id": i, "method": m, "params": p}
                           for i, (m, p) in enumerate(calls)]).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "Authorization": auth})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        out = [None] * len(calls)
        for d in data:
            if d.get("error") is None:
                out[d["id"]] = d.get("result")
        return out
    except Exception:
        return None


def msg_extract(block):
    """Les messages d'un bloc (getblock au niveau 2) : une sortie OP_RETURN = "RDTM" + texte UTF-8."""
    out = []
    for tx in block.get("tx", []):
        for vo in tx.get("vout", []):
            spk = vo.get("scriptPubKey") or {}
            if spk.get("type") != "nulldata":
                continue
            parts = str(spk.get("asm", "")).split()
            if len(parts) != 2 or parts[0] != "OP_RETURN":
                continue
            h = parts[1]
            if len(h) % 2 or len(h) > 8192 or not h.lower().startswith(MSG_MAGIC_HEX):
                continue
            try:
                text = bytes.fromhex(h)[len(MSG_MAGIC):].decode("utf-8")
            except ValueError:
                continue
            text = clean_msg(text)
            if not text:
                continue
            if len(text) > MSG_MAX:
                text = text[:MSG_MAX - 1] + "…"
            out.append({"txid": tx.get("txid"), "height": block.get("height"), "time": block.get("time"),
                        "i": len(out), "text": text})
            break                                      # un message par transaction
    return out


def msg_scan_once():
    """Lit les nouveaux blocs (400 au plus par tour). Renvoie True si le tour s'est bien passe."""
    info = rpc("getblockchaininfo", timeout=10)
    if not info:
        return False
    tip = int(info.get("blocks", 0))
    with _M_LOCK:
        scanned = _M["scanned"]
        recent = dict(_M["recent"])
    if scanned is not None:
        # reorganisation : le dernier bloc lu fait-il toujours partie de la chaine ?
        found = None
        for h in range(min(scanned, tip), max(-1, scanned - 40), -1):
            cur = rpc("getblockhash", [h], timeout=10)
            if cur is None:
                return False
            if recent.get(h) == cur:
                found = h
                break
        with _M_LOCK:
            if found is None:
                _M.update(items=[], scanned=None, recent={}, ready=False)
                scanned = None
            elif found < scanned:
                _M["items"] = [m for m in _M["items"] if m["height"] <= found]
                _M["scanned"] = scanned = found
    start = max(0, tip - MSG_BACK + 1) if scanned is None else scanned + 1
    end = min(tip, start + 399)
    if scanned is None:
        with _M_LOCK:
            _M["from"] = start
    new, newrecent = [], {}
    if start <= end:
        hashes = rpc_batch([("getblockhash", [h]) for h in range(start, end + 1)], timeout=30)
        if not hashes or None in hashes:
            return False
        for k in range(0, len(hashes), 100):
            blocks = rpc_batch([("getblock", [bh, 2]) for bh in hashes[k:k + 100]], timeout=60)
            if not blocks or None in blocks:
                return False
            for b in blocks:
                new += msg_extract(b)
        newrecent = {h: hashes[h - start] for h in range(start, end + 1)}
    with _M_LOCK:
        _M["items"] = sorted(_M["items"] + new, key=lambda m: (m["height"], m["i"]))[-MSG_KEEP:]
        if newrecent:
            r = dict(_M["recent"])
            r.update(newrecent)
            _M["recent"] = {h: v for h, v in r.items() if h > end - 40}
            _M["scanned"] = end
        _M["tip"] = tip
        _M["ready"] = end >= tip
    return True


def msg_loop():
    while True:
        ok = False
        try:
            ok = msg_scan_once()
        except Exception:
            pass
        with _M_LOCK:
            busy = not _M["ready"]
        time.sleep(0.3 if (ok and busy) else 8)


def _is_mine(txid):
    if txid in _MINE:
        return _MINE[txid]
    _, e = rpc_full("gettransaction", [txid], wallet=WALLET)
    if e is None:
        _MINE[txid] = True
    elif e.get("code") == -5:
        _MINE[txid] = False
    else:
        return False                                   # portefeuille indisponible : on ne retient rien
    return _MINE[txid]


def msg_view():
    with _M_LOCK:
        items, tip, ready, frm, scanned = list(_M["items"]), _M["tip"], _M["ready"], _M["from"], _M["scanned"]
    items.sort(key=lambda m: (m["height"], m["i"]), reverse=True)
    seen = {m["txid"] for m in items}
    feed = [{"txid": m["txid"], "time": m["time"], "height": m["height"], "conf": max(1, tip - m["height"] + 1),
             "text": m["text"], "mine": _is_mine(m["txid"])} for m in items[:MSG_SHOWN]]
    pend = []
    with _M_LOCK:
        mine_pending = list(_MPEND)
    for p in mine_pending:
        if p["txid"] in seen or time.time() - p["t"] > 3 * 86400:
            with _M_LOCK:
                if p in _MPEND:
                    _MPEND.remove(p)
            continue
        r, e = rpc_full("gettransaction", [p["txid"]], wallet=WALLET)
        if e is not None:
            if e.get("code") == -5:
                with _M_LOCK:
                    if p in _MPEND:
                        _MPEND.remove(p)
            continue
        pend.append({"txid": p["txid"], "text": p["text"], "sent": p["t"], "conf": int(r.get("confirmations", 0))})
    prog = None
    if not ready and frm is not None and tip > frm:
        prog = max(0.0, min(1.0, ((scanned if scanned is not None else frm) - frm + 1) / (tip - frm + 1)))
    return {"feed": feed, "pending": pend, "ready": ready, "from": frm, "to": tip, "progress": prog}


def msg_preview(text):
    if not isinstance(text, str):
        raise UserError("Message invalide.")
    if len(text) > 4000:
        raise UserError("Message trop long : %d caractères au plus." % MSG_MAX)
    t = clean_msg(text)
    if not t:
        raise UserError("Écris un message.")
    if len(t) > MSG_MAX:
        raise UserError("Message trop long : %d caractères au plus (le tien en fait %d)." % (MSG_MAX, len(t)))
    try:
        data = MSG_MAGIC + t.encode("utf-8")
    except UnicodeEncodeError:
        raise UserError("Ce message contient des caractères invalides.")
    info, _ = rpc_full("getblockchaininfo")
    if not info:
        raise UserError("Le nœud ne répond pas. Vérifie qu'il est démarré dans Umbrel.")
    if info.get("headers", 0) > info.get("blocks", 0) + 1 and info.get("initialblockdownload"):
        raise UserError("Le nœud n'est pas encore à jour : attends la fin de la synchronisation.")
    rate = fee_rate()
    res, e = rpc_full("walletcreatefundedpsbt",
                      [[], [{"data": data.hex()}], 0, {"fee_rate": plain(rate), "replaceable": False}], wallet=WALLET)
    if e:
        raise UserError(human_error(e))
    fee = Decimal(res["fee"])
    now = time.time()
    for k in [k for k, p in _PREVM.items() if now - p["t"] > 300]:
        _PREVM.pop(k, None)
    token = secrets.token_hex(16)
    _PREVM[token] = {"text": t, "rate": plain(rate), "t": now}
    return {"token": token, "text": t, "chars": len(t), "bytes": len(data), "fee": fmt_rdt(fee)}


def _broadcast_data(data_hex, rate):
    """Cree, signe, controle puis diffuse une transaction qui porte une donnee (OP_RETURN). Le portefeuille doit
    etre deverrouille. La transaction n'est confiee au portefeuille qu'apres avoir ete acceptee par le noeud."""
    r, e = rpc_full("walletcreatefundedpsbt",
                    [[], [{"data": data_hex}], 0, {"fee_rate": rate, "replaceable": False}], wallet=WALLET)
    if e:
        raise UserError(human_error(e))
    s, e = rpc_full("walletprocesspsbt", [r["psbt"], True], wallet=WALLET)
    if e:
        raise UserError(human_error(e))
    f, e = rpc_full("finalizepsbt", [s["psbt"]])
    if e or not f or not f.get("complete") or not f.get("hex"):
        raise UserError("Le portefeuille n'a pas pu signer la transaction.")
    raw = f["hex"]
    t, e = rpc_full("testmempoolaccept", [[raw]])
    if e:
        raise UserError(human_error(e))
    if not t or not t[0].get("allowed"):
        raise UserError(human_error({"code": 0, "message": str((t or [{}])[0].get("reject-reason", "refusée"))}))
    txid, e = rpc_full("sendrawtransaction", [raw])
    if e:
        raise UserError(human_error(e))
    return str(txid)


def msg_send(token, passphrase):
    now = time.time()
    token = str(token or "")
    if not isinstance(passphrase, str) or len(passphrase) > 1024:
        raise UserError("Mot de passe invalide.")
    with _WLOCK:
        p = _PREVM.get(token)
        if not p or now - p["t"] > 300:
            _PREVM.pop(token, None)
            raise UserError("Cet aperçu a expiré ou a déjà servi. Recommence depuis le début.")
        wi, e = rpc_full("getwalletinfo", wallet=WALLET)
        if e:
            raise UserError(human_error(e))
        unlocked = False
        if "unlocked_until" in wi:
            recent = [t for t in _FAILS if now - t < 600]
            _FAILS[:] = recent
            if len(recent) >= 5:
                raise UserError("Trop d'essais de mot de passe. Réessaie dans quelques minutes.")
            if not passphrase:
                raise UserError("Écris le mot de passe de ton portefeuille.")
            _, e = rpc_full("walletpassphrase", [passphrase, 30], wallet=WALLET)
            if e:
                if e.get("code") == -14:
                    _FAILS.append(now)
                raise UserError(human_error(e))
            unlocked = True
        _PREVM.pop(token, None)                   # a usage unique : consomme juste avant l'envoi
        try:
            txid = _broadcast_data((MSG_MAGIC + p["text"].encode("utf-8")).hex(), p["rate"])
        finally:
            if unlocked:
                rpc_full("walletlock", wallet=WALLET)
    with _M_LOCK:
        _MPEND.append({"txid": txid, "text": p["text"], "t": time.time()})
        del _MPEND[:-20]
    _MINE[txid] = True
    _cache["t"] = 0
    return str(txid)


VIEWS = {"node": view_node, "dashboard": view_dashboard, "mempool": view_mempool, "ckpool": view_ckpool,
         "wallet": view_wallet}

_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()


def build_state():
    try:
        secs, info = VIEWS.get(VIEW, view_dashboard)()
        tip = None
        for s in secs:
            if s.get("type") == "chain" and s["blocks"]:
                tip = s["blocks"][-1]
        err = None
    except Exception as e:           # ne jamais planter la page
        secs, info, tip, err = [note("err", "Erreur interne : %s" % e, "bad")], {}, None, str(e)
    return {"view": VIEW, "title": TITLES.get(VIEW, "Radthal"), "subtitle": SUBTITLES.get(VIEW, ""),
            "ok": bool(info), "now": time.time(), "error": err, "sections": secs}


def get_state():
    with _lock:
        # la page portefeuille interroge un portefeuille qui peut etre tres gros : on la rafraichit moins souvent
        if _cache["data"] is None or time.time() - _cache["t"] > (6.0 if VIEW == "wallet" else 2.0):
            _cache["data"] = build_state()
            _cache["t"] = time.time()
        return _cache["data"]


# ---------------------------------------------------------------- page

HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>__TITLE__</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 256 256%27%3E%3Crect width=%27256%27 height=%27256%27 rx=%2740%27 fill=%27%230b1220%27/%3E%3Cpath d=%27M128 36L206 80V166L128 210L50 166V80ZM50 80L128 124L206 80M128 124V210%27 fill=%27none%27 stroke=%27%23ff7a2f%27 stroke-width=%2712%27 stroke-linejoin=%27round%27/%3E%3C/svg%3E">
<style>
:root{--bg1:#090d15;--bg2:#111827;--card:#161e2e;--edge:#2e3a50;--acc:#ff7a2f;--acc2:#ffb25a;--tx:#f2f4f8;--mu:#96a2b4;--good:#54d68c;--bad:#ff6b62;--warn:#ffb25a}
*{box-sizing:border-box}
html{background:var(--bg1)}
body{margin:0;min-height:100vh;color:var(--tx);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 background:radial-gradient(ellipse 80% 40% at 50% 108%,rgba(255,122,47,.16),transparent 70%),linear-gradient(180deg,var(--bg1),var(--bg2)) fixed;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:20px 16px 36px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:12px}
.brand svg{width:38px;height:38px;flex:none}
h1{font-size:24px;font-weight:800;letter-spacing:-.01em;margin:0}
.sub{color:var(--mu);font-size:14px;margin:2px 0 0}
.status{display:flex;align-items:center;gap:10px;color:var(--mu);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--edge);background:var(--card);border-radius:999px;padding:5px 12px;font-size:12px;font-weight:600;color:var(--tx)}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--mu);display:block}
.pill.ok i{background:var(--good);box-shadow:0 0 8px var(--good)}
.pill.ko i{background:var(--bad);box-shadow:0 0 8px var(--bad)}
.sec{margin-top:22px}
.sec>h2{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--mu);margin:0 0 10px}
.mu{color:var(--mu)} .good{color:var(--good)} .bad{color:var(--bad)} .warn{color:var(--warn)}
/* chaine de blocs */
.band{display:flex;overflow-x:auto;padding:14px 4px 16px;scroll-behavior:smooth;scrollbar-width:thin;scrollbar-color:var(--edge) transparent}
.blk{flex:0 0 auto;position:relative;width:104px;margin-right:22px;padding:9px 0 10px;text-align:center;background:var(--card);border:1.5px solid var(--edge);border-radius:12px;cursor:default}
.blk::after{content:"";position:absolute;left:100%;top:50%;width:22px;height:3px;margin-top:-1.5px;background:var(--edge)}
.blk:last-child{margin-right:6px}.blk:last-child::after{display:none}
.blk small{display:block;font-size:10px;font-weight:700;letter-spacing:.14em;color:var(--mu)}
.blk b{display:block;font-size:19px;font-weight:800;font-variant-numeric:tabular-nums;margin-top:1px}
.blk em{display:block;font-style:normal;font-size:11px;color:var(--mu);margin-top:3px}
.blk.mine{border-color:var(--acc);background:#2a1a12;box-shadow:0 0 22px rgba(255,122,47,.38)}
.blk.mine small{color:var(--acc2)}.blk.mine::after{background:var(--acc)}
.blk.tip{border-color:var(--acc2)}
.blk.enter{animation:enter .9s cubic-bezier(.2,.8,.2,1)}
@keyframes enter{0%{opacity:0;transform:translateX(46px) scale(.9)}55%{box-shadow:0 0 30px rgba(255,178,90,.7)}100%{opacity:1;transform:none}}
.chainfoot{color:var(--mu);font-size:13px;margin-top:2px}
/* cartes */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:13px 15px;position:relative;overflow:hidden;transition:border-color .5s,box-shadow .5s}
.card .lab{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--mu)}
.card .v{font-size:27px;font-weight:800;letter-spacing:-.01em;margin-top:5px;font-variant-numeric:tabular-nums;line-height:1.15}
.card .v .n{white-space:nowrap}
.card .v.long{font-size:21px}
.card .v .u{font-size:14px;font-weight:600;color:var(--mu);margin-left:4px;white-space:nowrap;display:inline-block}
.card .s{font-size:12px;color:var(--mu);margin-top:5px;line-height:1.35}
.card .v.t{font-size:20px;word-break:normal;overflow-wrap:anywhere}
.card.accent .v{color:var(--acc)}
.card.good .v{color:var(--good)}.card.warn .v{color:var(--warn)}.card.bad .v{color:var(--bad)}
.card.bump{border-color:var(--acc);box-shadow:0 0 18px rgba(255,122,47,.25)}
/* mineur */
.miner{display:grid;grid-template-columns:minmax(240px,340px) 1fr;gap:22px;align-items:center;background:var(--card);border:1px solid var(--edge);border-radius:16px;padding:18px 20px}
@media(max-width:640px){.miner{grid-template-columns:1fr}}
.asic{width:100%;height:auto;display:block}
.asic .body{fill:#233047;stroke:#475a7c;stroke-width:2}
.asic .ring{fill:#0b1220;stroke:#475a7c;stroke-width:2}
.asic .blade{stroke:#7d8cab;stroke-width:5;stroke-linecap:round}
.asic .hub{fill:#475a7c}
.asic .strip{fill:var(--acc);opacity:.9}
.asic .led{fill:var(--mu)}
.miner.on .led{fill:var(--good);animation:blink 1.6s infinite}
.miner.idle .led{fill:var(--warn)}
.miner.off .led{fill:var(--bad)}
.miner.off .strip,.miner.idle .strip{opacity:.25}
.fan{transform-box:fill-box;transform-origin:center;animation:spin 1.1s linear infinite;animation-play-state:paused}
.miner.on .fan{animation-play-state:running}
.miner.on .fan.f2{animation-duration:1.3s}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes blink{50%{opacity:.35}}
.hashes{font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#6c7a93;text-align:center;margin-top:8px;overflow:hidden;height:5em;word-break:break-all}
.hashes small{display:block;font:11px Inter,system-ui,sans-serif;color:var(--mu);opacity:.8;margin-top:2px}
.mstate{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:700}
.mstate i{width:9px;height:9px;border-radius:50%;background:var(--mu)}
.miner.on .mstate i{background:var(--good);box-shadow:0 0 8px var(--good)}
.miner.idle .mstate i{background:var(--warn)}.miner.off .mstate i{background:var(--bad)}
.big{font-size:54px;font-weight:900;letter-spacing:-.02em;color:var(--acc);font-variant-numeric:tabular-nums;line-height:1.05;margin-top:6px}
.big .u{font-size:20px;font-weight:700;color:var(--acc2);margin-left:6px}
.tries{color:var(--tx);font-size:16px;margin-top:6px}
.mfoot{color:var(--mu);font-size:13px;margin-top:10px}
/* tableaux, listes */
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--edge);border-radius:14px}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 14px;text-align:left;font-size:13px;white-space:nowrap}
th{color:var(--mu);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;border-bottom:1px solid var(--edge)}
td{border-bottom:1px solid rgba(46,58,80,.55);font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
@media(max-width:640px){th,td{padding:8px 9px;font-size:12px}th{font-size:10px;letter-spacing:.04em}}
.empty{padding:16px;color:var(--mu);font-size:13px}
.kv td:first-child{color:var(--mu);width:44%}
.bars{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:14px 16px}
.bar{display:grid;grid-template-columns:110px 1fr 90px;gap:10px;align-items:center;font-size:12px;margin:6px 0}
.bar .track{height:10px;border-radius:5px;background:#0e1626;overflow:hidden}
.bar .fill{height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));border-radius:5px;transition:width .8s}
.conn{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:6px 16px 14px}
.crow{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid rgba(46,58,80,.55);flex-wrap:wrap}
.crow .l{color:var(--mu);font-size:12px;width:110px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.crow code{flex:1;min-width:180px;background:#0e1626;border:1px solid var(--edge);border-radius:8px;padding:6px 10px;font-size:13px;word-break:break-all}
button.cp{background:#0e1626;color:var(--tx);border:1px solid var(--edge);border-radius:8px;padding:6px 12px;font:600 12px Inter,system-ui,sans-serif;cursor:pointer}
button.cp:hover{border-color:var(--acc);color:var(--acc2)}
.crow code.hint{font:13px Inter,system-ui,sans-serif;color:var(--mu);background:none;border-style:dashed}
@media(max-width:640px){.crow .l{width:100%}.crow code{min-width:0;flex:1 1 100%}}
.cwarn{color:var(--warn);font-size:12px;margin-top:10px}
.note{margin-top:22px;padding:12px 16px;border-radius:12px;border:1px solid var(--edge);background:var(--card);font-size:14px}
.note.bad{border-color:var(--bad);color:var(--bad)}.note.warn{border-color:var(--warn);color:var(--warn)}
/* portefeuille */
[hidden]{display:none!important}
.small{font-size:12px;line-height:1.45}
@media(max-width:480px){.cards{grid-template-columns:1fr}}
.recv{display:grid;grid-template-columns:minmax(170px,220px) 1fr;gap:22px;align-items:center;background:var(--card);border:1px solid var(--edge);border-radius:16px;padding:18px 20px}
@media(max-width:640px){.recv{grid-template-columns:1fr;justify-items:center}.rr{width:100%}}
.qr{background:#fff;border-radius:12px;padding:6px;width:100%;max-width:220px;aspect-ratio:1/1}
.qr svg{display:block;width:100%;height:100%}
.addr{display:block;background:#0e1626;border:1px solid var(--edge);border-radius:8px;padding:10px 12px;margin:6px 0 10px;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
.btnrow{display:flex;gap:8px;flex-wrap:wrap}
.rmsg{font-size:13px;margin-top:10px;min-height:1em}
.hint{margin-top:10px}
.send{background:var(--card);border:1px solid var(--edge);border-radius:16px;padding:6px 20px 20px}
.send label{display:block;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--mu);margin:14px 0 6px}
.send label.chk{text-transform:none;letter-spacing:0;font-size:13px;font-weight:500;color:var(--tx);display:flex;align-items:center;gap:8px;margin:12px 0 8px}
.send input[type=text],.send input[type=password]{width:100%;background:#0e1626;border:1px solid var(--edge);border-radius:10px;color:var(--tx);padding:12px;font:15px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.send input:focus{outline:none;border-color:var(--acc)}
.send input:disabled{opacity:.45}
.btn{background:linear-gradient(90deg,var(--acc),var(--acc2));color:#1a0f08;border:0;border-radius:10px;padding:12px 20px;margin-top:14px;font:800 15px Inter,system-ui,sans-serif;cursor:pointer}
.btn:hover:not(:disabled){filter:brightness(1.08)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn.ghost{background:#0e1626;color:var(--tx);border:1px solid var(--edge);font-weight:600}
.ferr{color:var(--bad);font-size:13px;margin-top:10px}
.ptitle{font-size:16px;font-weight:800;margin:14px 0 4px}
.prow{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:9px 0;border-bottom:1px solid rgba(46,58,80,.55);font-size:14px;flex-wrap:wrap}
.prow.col{display:block}
.prow b{font-variant-numeric:tabular-nums}
.prow b.big2{font-size:22px;color:var(--acc)}
.dmsg{color:var(--good);font-weight:700;margin:14px 0 4px}
details.prot{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:4px 18px 16px}
details.prot summary{cursor:pointer;font-weight:700;font-size:14px;padding:12px 0 6px}
details.prot p{margin:8px 0;font-size:13px;line-height:1.5;color:var(--mu)}
.toast{white-space:pre-line;display:flex;gap:14px;align-items:center;position:fixed;left:50%;bottom:24px;transform:translateX(-50%);width:min(560px,92vw);background:#16301f;border:1px solid var(--good);color:var(--tx);padding:12px 18px;border-radius:12px;font-size:14px;z-index:9;box-shadow:0 8px 30px rgba(0,0,0,.5)}
/* messages */
.send textarea{width:100%;background:#0e1626;border:1px solid var(--edge);border-radius:10px;color:var(--tx);padding:12px;font:15px/1.5 Inter,system-ui,sans-serif;resize:vertical}
.send textarea:focus{outline:none;border-color:var(--acc)}
.msgs{background:var(--card);border:1px solid var(--edge);border-radius:14px;margin-top:10px}
.msg{padding:13px 16px;border-bottom:1px solid rgba(46,58,80,.55)}
.msg:last-child{border-bottom:0}
.mtop{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.mdt{font-weight:700;font-size:13px}
.mtxt{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px;line-height:1.5}
.mtxt.mprev{background:#0e1626;border:1px solid var(--edge);border-radius:10px;padding:12px;margin:6px 0 10px}
.mmeta{margin-top:7px;overflow-wrap:anywhere}
.mstat{margin-top:10px}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:11px;font-weight:700;border:1px solid var(--edge);color:var(--mu)}
.badge.good{border-color:var(--good);color:var(--good)}.badge.warn{border-color:var(--warn);color:var(--warn)}.badge.bad{border-color:var(--bad);color:var(--bad)}
footer{margin-top:34px;color:#6b7890;font-size:12px;text-align:center}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style></head><body><div class="wrap">
<header class="top">
 <div class="brand">
  <svg viewBox="0 0 256 256" aria-hidden="true"><rect width="256" height="256" rx="40" fill="#0b1220"/><g fill="none" stroke="#ff7a2f" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"><path d="M128 36L206 80V166L128 210L50 166V80Z"/><path d="M50 80L128 124L206 80M128 124V210"/></g></svg>
  <div><h1 id="ttl">__TITLE__</h1><p class="sub" id="sub"></p></div>
 </div>
 <div class="status"><span class="pill" id="pill"><i></i><span id="pilltx">Connexion…</span></span><span id="upd"></span></div>
</header>
<main id="root"></main>
<footer>Radthal · projet expérimental, sans valeur garantie · mise à jour automatique toutes les 3 secondes</footer>
</div>
<script>
"use strict";
const VIEW="__VIEW__";
const reduce=window.matchMedia&&matchMedia("(prefers-reduced-motion: reduce)").matches;
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e;};
let offset=0, lastOk=0;
const nowS=()=>Date.now()/1000+offset;
const fmt=(n,d)=>Number(n).toLocaleString("fr-FR",{minimumFractionDigits:d,maximumFractionDigits:d});
function ago(ts){const d=Math.max(0,Math.floor(nowS()-ts));
 if(d<60)return d+" s"; if(d<3600)return Math.floor(d/60)+" min "+String(d%60).padStart(2,"0")+" s";
 if(d<86400)return Math.floor(d/3600)+" h "+String(Math.floor(d%3600/60)).padStart(2,"0"); return Math.floor(d/86400)+" j "+Math.floor(d%86400/3600)+" h";}
function ageSpan(ts){const s=$("span","");s.dataset.age=ts;s.textContent=ago(ts);return s;}

/* compteur anime */
function setNum(el,num,dec){
 if(el._raf)cancelAnimationFrame(el._raf);
 const from=el._shown!==undefined?el._shown:0; el._num=num;
 if(reduce||from===num){el._shown=num;el.textContent=fmt(num,dec);return false;}
 const t0=performance.now(),dur=900;
 const step=t=>{const k=Math.min(1,(t-t0)/dur),e=1-Math.pow(1-k,3);
  el._shown=from+(num-from)*e;el.textContent=fmt(el._shown,dec);
  if(k<1)el._raf=requestAnimationFrame(step);else{el._shown=num;el.textContent=fmt(num,dec);}};
 el._raf=requestAnimationFrame(step);return true;
}

const reg={};      /* id -> {el, type, ...} */
const R={};

/* ---- cartes */
R.cards=(rec,d)=>{
 if(!rec.el){rec.el=$("section","sec");if(d.title)rec.el.append($("h2","",d.title));rec.grid=$("div","cards");rec.el.append(rec.grid);rec.map={};}
 const seen=new Set();
 d.items.forEach(it=>{
  seen.add(it.id);let c=rec.map[it.id];
  if(!c){c={el:$("div","card"),lab:$("div","lab"),v:$("div","v"),n:$("span","n"),u:$("span","u"),s:$("div","s")};
   c.v.append(c.n,c.u);c.el.append(c.lab,c.v,c.s);rec.map[it.id]=c;rec.grid.append(c.el);}
  c.lab.textContent=it.label;c.s.textContent=it.sub||"";c.s.style.display=it.sub?"":"none";
  c.el.className="card"+(it.accent?" accent":"")+(it.tone?" "+it.tone:"");
  c.u.textContent=it.unit||"";
  if(it.num!==null&&it.num!==undefined){const ch=setNum(c.n,it.num,it.dec||0);if(ch&&c.hadValue){c.el.classList.add("bump");setTimeout(()=>c.el.classList.remove("bump"),1300);}c.hadValue=true;}
  else{c.n.textContent=it.value!=null?it.value:"-";c.n._shown=undefined;}
  c.v.classList.toggle("t",!(it.num!==null&&it.num!==undefined));
  c.v.classList.toggle("long",it.num!==null&&it.num!==undefined&&fmt(it.num,it.dec||0).length>9);
 });
 for(const k in rec.map)if(!seen.has(k)){rec.map[k].el.remove();delete rec.map[k];}
 order(rec.grid,d.items.map(i=>rec.map[i.id].el));
};
function order(parent,els){let i=0;for(const e of els){if(parent.children[i]!==e)parent.insertBefore(e,parent.children[i]||null);i++;}}

/* ---- chaine de blocs */
R.chain=(rec,d)=>{
 if(!rec.el){rec.el=$("section","sec");rec.el.append($("h2","",d.title));rec.band=$("div","band");rec.foot=$("div","chainfoot");rec.el.append(rec.band,rec.foot);rec.map=new Map();rec.first=true;}
 const seen=new Set();let added=false;
 d.blocks.forEach(b=>{
  seen.add(b.hash);let e=rec.map.get(b.hash);
  if(!e){e=$("div","blk");e.append($("small","","BLOC"),$("b","",fmt(b.h,0)),$("em","",""));rec.map.set(b.hash,e);rec.band.append(e);
   if(!rec.first&&!reduce)e.classList.add("enter");added=true;}
  e.classList.toggle("mine",!!b.mine);
  e.lastChild.textContent=b.mine?"trouvé par toi":(b.ntx+" tx");
  e.title="Hash : "+b.hash+"\n"+b.ntx+" transaction(s) · "+b.size+" octets";
 });
 for(const [h,e] of rec.map)if(!seen.has(h)){e.remove();rec.map.delete(h);}
 order(rec.band,d.blocks.map(b=>rec.map.get(b.hash)));
 [...rec.band.children].forEach((e,i,a)=>e.classList.toggle("tip",i===a.length-1));
 const last=d.blocks[d.blocks.length-1];rec.last=last;
 if(added||rec.first){rec.scrollEnd=rec.first?"auto":"smooth";}
 rec.first=false;
 rec.foot.textContent="";
 if(last){rec.foot.append("Dernier bloc : ",$("b","",fmt(last.h,0)),", trouvé il y a ",ageSpan(last.time));}
};

/* ---- mineur */
const ASIC=`<svg class="asic" viewBox="0 0 340 170" aria-hidden="true"><rect class="body" x="4" y="4" width="332" height="162" rx="14"/>
<g><circle class="ring" cx="92" cy="80" r="62"/><g class="fan f1"><g transform="translate(92 80)"><circle class="hub" r="7"/>
<path class="blade" d="M0 0L-6 -50M0 0L48 -14M0 0L30 42M0 0L-34 38M0 0L-50 -12"/></g></g></g>
<g><circle class="ring" cx="236" cy="80" r="62"/><g class="fan f2"><g transform="translate(236 80)"><circle class="hub" r="7"/>
<path class="blade" d="M0 0L-6 -50M0 0L48 -14M0 0L30 42M0 0L-34 38M0 0L-50 -12"/></g></g></g>
<rect class="strip" x="20" y="152" width="300" height="5" rx="2.5"/><circle class="led" cx="318" cy="20" r="5"/></svg>`;
const HEX="0123456789abcdef";
function rhash(){let s="";for(let i=0;i<32;i++)s+=HEX[Math.random()*16|0];return s;}
R.miner=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Mon mineur"));
  rec.box=$("div","miner");const left=$("div","");left.innerHTML=ASIC;
  rec.hashes=$("div","hashes");left.append(rec.hashes);
  const right=$("div","");rec.st=$("div","mstate");rec.st.append($("i"),$("span"));
  rec.big=$("div","big");rec.bn=$("span","n","0");rec.bu=$("span","u","");rec.big.append(rec.bn,rec.bu);
  rec.tries=$("div","tries");rec.foot=$("div","mfoot");
  right.append(rec.st,rec.big,rec.tries,rec.foot);rec.box.append(left,right);rec.el.append(rec.box);
  rec.hl=$("div","");rec.hl.style.whiteSpace="pre";rec.hl.textContent=[rhash(),rhash(),rhash()].join("\n");
  rec.hashes.append(rec.hl,$("small","","illustration : les nombres essayés, le vrai compte est à droite"));
  rec.timer=setInterval(()=>{if(rec.state!=="on"||reduce||document.hidden)return;rec.hl.textContent=[rhash(),rhash(),rhash()].join("\n");},110);
 }
 rec.state=d.state;rec.box.className="miner "+d.state;rec.st.lastChild.textContent=d.label;
 rec.big.style.display=d.state==="off"?"none":"";
 if(d.state!=="off"){setNum(rec.bn,d.hashrate.num,2);rec.bu.textContent=d.hashrate.unit;}
 rec.tries.textContent=d.tries||"";
 rec.foot.textContent="";
 if(d.state==="off")rec.foot.append("ckpool n'a encore rien écrit. Vérifie que l'app ckpool est démarrée.");
 else{rec.foot.append(d.workers+(d.workers>1?" mineurs connectés":" mineur connecté"));if(d.lastshare){rec.foot.append(" · dernière part il y a ",ageSpan(d.lastshare));}}
};

/* ---- tableaux */
function cell(td,c){
 if(c&&typeof c==="object"){ if(c.age)td.append(ageSpan(c.age)); else{td.textContent=c.t;if(c.cls)td.className=c.cls;if(c.title)td.title=c.title;} }
 else td.textContent=c==null?"-":c;
}
R.table=(rec,d)=>{
 if(!rec.el){rec.el=$("section","sec");rec.el.append($("h2","",d.title));rec.body=$("div","");rec.el.append(rec.body);}
 const sig=JSON.stringify(d.rows);if(rec.sig===sig)return;rec.sig=sig;
 rec.body.textContent="";
 if(!d.rows.length){rec.body.append($("div","tw empty",d.empty));return;}
 const w=$("div","tw"),t=$("table"),h=$("tr");d.head.forEach(x=>h.append($("th","",x)));t.append(h);
 d.rows.forEach(r=>{const tr=$("tr");r.forEach(c=>{const td=$("td");cell(td,c);tr.append(td);});t.append(tr);});
 w.append(t);rec.body.append(w);
};
R.kv=(rec,d)=>{
 if(!rec.el){rec.el=$("section","sec");rec.el.append($("h2","",d.title));}
 const sig=JSON.stringify(d.rows);if(rec.sig===sig)return;rec.sig=sig;
 if(rec.w)rec.w.remove();rec.w=$("div","tw kv");const t=$("table");
 d.rows.forEach(r=>{const tr=$("tr");const a=$("td");const b=$("td");cell(a,r[0]);cell(b,r[1]);tr.append(a,b);t.append(tr);});
 rec.w.append(t);rec.el.append(rec.w);
};
R.bars=(rec,d)=>{
 if(!rec.el){rec.el=$("section","sec");rec.el.append($("h2","",d.title));rec.box=$("div","bars");rec.el.append(rec.box);rec.rows=[];
  d.items.forEach(()=>{const r=$("div","bar"),l=$("span"),tr=$("div","track"),f=$("div","fill"),n=$("span","mu");tr.append(f);r.append(l,tr,n);rec.box.append(r);rec.rows.push({l,f,n});});}
 const max=Math.max(1,...d.items.map(i=>i.count));
 d.items.forEach((it,i)=>{const r=rec.rows[i];r.l.textContent=it.label;r.f.style.width=(it.count/max*100)+"%";r.n.textContent=it.count+" tx · "+it.size+" vB";});
};
R.connect=(rec,d)=>{
 if(rec.el)return;
 rec.el=$("section","sec");rec.el.append($("h2","",d.title));const box=$("div","conn");
 d.items.forEach(it=>{const r=$("div","crow"),v=it.value.replace("{host}",location.hostname),c=$("code",it.copy?"":"hint",v);
  r.append($("span","l",it.label),c);
  if(it.copy){const b=$("button","cp","Copier");b.onclick=()=>{const ok=()=>{b.textContent="Copié ✓";setTimeout(()=>b.textContent="Copier",1500);};
   if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(v).then(ok,()=>{});else{const t=$("textarea");t.value=v;document.body.append(t);t.select();try{document.execCommand("copy");ok();}catch(e){}t.remove();}};r.append(b);}
  box.append(r);});
 if(d.warn)box.append($("div","cwarn",d.warn));
 rec.el.append(box);
};
R.note=(rec,d)=>{ if(!rec.el)rec.el=$("div","note"); rec.el.className="note"+(d.tone?" "+d.tone:""); rec.el.textContent=d.text; };

/* ---- generateur de QR code (mode octets, correction d'erreur M, versions 1 a 12) */
const QR=(()=>{
 const ECC=[10,16,26,18,24,16,18,22,22,26,30,22], BLK=[1,1,1,2,2,4,4,4,5,5,5,8];
 const raw=v=>{let r=(16*v+128)*v+64;if(v>=2){const n=Math.floor(v/7)+2;r-=(25*n-10)*n-55;if(v>=7)r-=36;}return r;};
 const dcw=v=>Math.floor(raw(v)/8)-ECC[v-1]*BLK[v-1];
 const mul=(x,y)=>{let z=0;for(let i=7;i>=0;i--){z=(z<<1)^((z>>>7)*0x11D);z^=((y>>>i)&1)*x;}return z;};
 const divisor=deg=>{const r=new Array(deg).fill(0);r[deg-1]=1;let root=1;
  for(let i=0;i<deg;i++){for(let j=0;j<deg;j++){r[j]=mul(r[j],root);if(j+1<deg)r[j]^=r[j+1];}root=mul(root,2);}return r;};
 const remainder=(data,div)=>{const r=div.map(()=>0);
  for(const b of data){const f=b^r.shift();r.push(0);div.forEach((c,i)=>{r[i]^=mul(c,f);});}return r;};
 const bit=(x,i)=>((x>>>i)&1)!==0;
 function make(text){
  const bytes=[...new TextEncoder().encode(text)];
  let ver=0;
  for(let v=1;v<=12;v++){const cc=v<10?8:16;if(4+cc+8*bytes.length<=dcw(v)*8){ver=v;break;}}
  if(!ver)throw new Error("trop long");
  const bits=[];const put=(val,n)=>{for(let i=n-1;i>=0;i--)bits.push((val>>>i)&1);};
  put(4,4);put(bytes.length,ver<10?8:16);bytes.forEach(b=>put(b,8));
  const cap=dcw(ver)*8;put(0,Math.min(4,cap-bits.length));put(0,(8-bits.length%8)%8);
  for(let pad=0xEC;bits.length<cap;pad^=0xEC^0x11)put(pad,8);
  const data=[];for(let i=0;i<bits.length;i+=8){let b=0;for(let j=0;j<8;j++)b=(b<<1)|bits[i+j];data.push(b);}
  /* blocs + correction d'erreur */
  const nb=BLK[ver-1],el=ECC[ver-1],rawCw=Math.floor(raw(ver)/8),nShort=nb-rawCw%nb,shortLen=Math.floor(rawCw/nb);
  const div=divisor(el),blocks=[];
  for(let i=0,k=0;i<nb;i++){const dat=data.slice(k,k+shortLen-el+(i<nShort?0:1));k+=dat.length;
   const ecc=remainder(dat,div);if(i<nShort)dat.push(0);blocks.push(dat.concat(ecc));}
  const all=[];
  for(let i=0;i<blocks[0].length;i++)blocks.forEach((b,j)=>{if(i!==shortLen-el||j>=nShort)all.push(b[i]);});
  /* matrice */
  const size=ver*4+17,m=[],fn=[];
  for(let y=0;y<size;y++){m.push(new Array(size).fill(false));fn.push(new Array(size).fill(false));}
  const setF=(x,y,d)=>{m[y][x]=d;fn[y][x]=true;};
  for(let i=0;i<size;i++){setF(6,i,i%2===0);setF(i,6,i%2===0);}
  const finder=(cx,cy)=>{for(let dy=-4;dy<=4;dy++)for(let dx=-4;dx<=4;dx++){const d=Math.max(Math.abs(dx),Math.abs(dy)),x=cx+dx,y=cy+dy;
   if(x>=0&&x<size&&y>=0&&y<size)setF(x,y,d!==2&&d!==4);}};
  finder(3,3);finder(size-4,3);finder(3,size-4);
  const pos=[];if(ver>1){const n=Math.floor(ver/7)+2,step=Math.ceil((ver*4+4)/(n*2-2))*2;pos.push(6);for(let p=size-7;pos.length<n;p-=step)pos.splice(1,0,p);}
  pos.forEach((px,i)=>pos.forEach((py,j)=>{if((i===0&&j===0)||(i===0&&j===pos.length-1)||(i===pos.length-1&&j===0))return;
   for(let dy=-2;dy<=2;dy++)for(let dx=-2;dx<=2;dx++)setF(px+dx,py+dy,Math.max(Math.abs(dx),Math.abs(dy))!==1);}));
  const fmt=mask=>{const d=0<<3|mask;let r=d;for(let i=0;i<10;i++)r=(r<<1)^((r>>>9)*0x537);const b=(d<<10|r)^0x5412;
   for(let i=0;i<=5;i++)setF(8,i,bit(b,i));setF(8,7,bit(b,6));setF(8,8,bit(b,7));setF(7,8,bit(b,8));for(let i=9;i<15;i++)setF(14-i,8,bit(b,i));
   for(let i=0;i<8;i++)setF(size-1-i,8,bit(b,i));for(let i=8;i<15;i++)setF(8,size-15+i,bit(b,i));setF(8,size-8,true);};
  fmt(0);
  if(ver>=7){let r=ver;for(let i=0;i<12;i++)r=(r<<1)^((r>>>11)*0x1F25);const b=ver<<12|r;
   for(let i=0;i<18;i++){const c=bit(b,i),a=size-11+i%3,q=Math.floor(i/3);setF(a,q,c);setF(q,a,c);}}
  /* donnees en zigzag */
  let k=0;
  for(let right=size-1;right>=1;right-=2){if(right===6)right=5;
   for(let v=0;v<size;v++)for(let j=0;j<2;j++){const x=right-j,up=((right+1)&2)===0,y=up?size-1-v:v;
    if(!fn[y][x]&&k<all.length*8){m[y][x]=bit(all[k>>>3],7-(k&7));k++;}}}
  const mask=(n)=>{for(let y=0;y<size;y++)for(let x=0;x<size;x++){let inv;
   switch(n){case 0:inv=(x+y)%2===0;break;case 1:inv=y%2===0;break;case 2:inv=x%3===0;break;case 3:inv=(x+y)%3===0;break;
    case 4:inv=(Math.floor(x/3)+Math.floor(y/2))%2===0;break;case 5:inv=x*y%2+x*y%3===0;break;
    case 6:inv=(x*y%2+x*y%3)%2===0;break;default:inv=((x+y)%2+x*y%3)%2===0;}
   if(!fn[y][x]&&inv)m[y][x]=!m[y][x];}};
  const penalty=()=>{let p=0;
   const line=g=>{let run=1;for(let i=1;i<size;i++){if(g(i)===g(i-1)){run++;if(run===5)p+=3;else if(run>5)p++;}else run=1;}
    const s=[];for(let i=0;i<size;i++)s.push(g(i)?1:0);const t=s.join("");
    for(const pat of["10111010000","00001011101"]){let at=t.indexOf(pat);while(at>=0){p+=40;at=t.indexOf(pat,at+1);}}};
   for(let y=0;y<size;y++)line(i=>m[y][i]);for(let x=0;x<size;x++)line(i=>m[i][x]);
   for(let y=0;y<size-1;y++)for(let x=0;x<size-1;x++){const c=m[y][x];if(c===m[y][x+1]&&c===m[y+1][x]&&c===m[y+1][x+1])p+=3;}
   let dark=0;m.forEach(r=>r.forEach(c=>{if(c)dark++;}));const tot=size*size;
   p+=(Math.ceil(Math.abs(dark*20-tot*10)/tot)-1)*10;return p;};
  let best=0,bestP=Infinity;
  for(let n=0;n<8;n++){mask(n);fmt(n);const p=penalty();if(p<bestP){best=n;bestP=p;}mask(n);}
  mask(best);fmt(best);
  return m;
 }
 return {make};
})();
function qrSvg(text){
 const q=QR.make(text),n=q.length,pad=4;let d="";
 for(let y=0;y<n;y++)for(let x=0;x<n;x++)if(q[y][x])d+="M"+(x+pad)+","+(y+pad)+"h1v1h-1z";
 const s=n+2*pad;
 return '<svg viewBox="0 0 '+s+' '+s+'" shape-rendering="crispEdges" role="img" aria-label="QR code de ton adresse"><rect width="'+s+'" height="'+s+'" fill="#fff"/><path d="'+d+'" fill="#000"/></svg>';
}
async function post(path,body){
 try{
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),cache:"no-store"});
  return await r.json();
 }catch(e){return {ok:false,error:"Le serveur ne répond pas. Réessaie dans un instant."};}
}
function copyTxt(v,b,label){
 const ok=()=>{b.textContent="Copié ✓";setTimeout(()=>b.textContent=label,1500);};
 if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(v).then(ok,()=>{});
 else{const t=$("textarea");t.value=v;document.body.append(t);t.select();try{document.execCommand("copy");ok();}catch(e){}t.remove();}
}

/* ---- recevoir */
R.receive=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Recevoir des RDT"));
  const box=$("div","recv");rec.qr=$("div","qr");
  const right=$("div","rr");
  rec.code=$("code","addr");
  const row=$("div","btnrow");
  rec.cp=$("button","cp","Copier l'adresse");rec.nw=$("button","cp","Nouvelle adresse");
  rec.msg=$("div","rmsg mu");
  row.append(rec.cp,rec.nw);
  right.append($("div","mu small","Ton adresse de réception"),rec.code,row,rec.msg,
   $("div","mu small hint","Ce QR code contient seulement ton adresse : la personne qui t'envoie des RDT le scanne avec son portefeuille. Tu peux donner la même adresse plusieurs fois, mais en changer à chaque paiement protège mieux ta vie privée."));
  box.append(rec.qr,right);rec.el.append(box);
  rec.cp.onclick=()=>{if(rec.addr)copyTxt(rec.addr,rec.cp,"Copier l'adresse");};
  rec.nw.onclick=async()=>{rec.nw.disabled=true;const j=await post("api/newaddress",{});rec.nw.disabled=false;
   if(j.ok){rec.msg.textContent="Nouvelle adresse créée. L'ancienne reste valable.";poll();}else rec.msg.textContent=j.error||"Impossible de créer une adresse.";};
 }
 if(d.address!==rec.addr){rec.addr=d.address;
  if(d.address){try{rec.qr.innerHTML=qrSvg(d.address);}catch(e){rec.qr.textContent="QR indisponible";}}else rec.qr.textContent="";
  rec.code.textContent=d.address||"Adresse indisponible";rec.msg.textContent="";}
 if(d.used&&!rec.msg.textContent)rec.msg.textContent="Cette adresse a déjà reçu des RDT. Tu peux en créer une nouvelle : l'ancienne reste valable.";
};

let backupNote="";
function toast(t){const e=$("div","toast"),b=$("button","cp","OK");e.append($("div","",t),b);b.onclick=()=>e.remove();document.body.append(e);}

/* ---- proteger le portefeuille par un mot de passe */
R.protect=(rec,d)=>{
 if(rec.el)return;
 rec.el=$("section","sec");
 const dt=$("details","prot");dt.append($("summary","","Protéger mon portefeuille avec un mot de passe"));
 dt.append($("p","","Une fois protégé, le mot de passe sera demandé à chaque envoi. Réception, solde et minage continuent de fonctionner normalement."),
  $("p","warn","Attention : si tu perds ce mot de passe, tes RDT sont perdus pour toujours. Personne ne peut le retrouver, ni moi, ni le projet. Note-le sur papier et range-le avec ta sauvegarde."),
  $("p","","Important : la protection fait créer de nouvelles clés au portefeuille. Ton ancienne sauvegarde reste valable pour les RDT que tu as déjà, mais elle ne couvrira pas ce que tu recevras ensuite. La page crée donc automatiquement une nouvelle sauvegarde juste après : garde-la."));
 const box=$("div","send");box.style.cssText="border:0;padding:0;background:none";
 const a=$("input"),b=$("input");a.type=b.type="password";a.autocomplete=b.autocomplete="new-password";
 const lc=$("label","chk");const ck=$("input");ck.type="checkbox";lc.append(ck," J'ai noté mon mot de passe sur papier");
 const go=$("button","btn","Protéger mon portefeuille"),err=$("div","ferr");
 box.append($("label","","Choisis un mot de passe (8 caractères minimum)"),a,$("label","","Écris-le une seconde fois"),b,lc,go,err);
 dt.append(box);rec.el.append(dt);
 go.onclick=async()=>{err.textContent="";go.disabled=true;go.textContent="Protection en cours…";
  const j=await post("api/encrypt",{pass1:a.value,pass2:b.value,ack:ck.checked});
  go.disabled=false;go.textContent="Protéger mon portefeuille";
  if(!j.ok){err.textContent=j.error||"Erreur.";return;}
  a.value=b.value="";ck.checked=false;
  if(j.backup){backupNote="Sauvegarde créée : "+j.backup;toast("Portefeuille protégé ✓ Le mot de passe sera demandé à chaque envoi.\nNouvelle sauvegarde créée : "+j.backup+"\nElle est dans le dossier de sauvegarde du nœud (voir « Sauvegarde » en bas de page). Copie-la ailleurs que sur le Pi.");}
  else{backupNote=j.backup_error||"";toast("Portefeuille protégé ✓ Mais la sauvegarde automatique a échoué : "+(j.backup_error||"")+"\nFais une sauvegarde à la main avant d'aller plus loin.");}
  poll();};
};

/* ---- sauvegarde */
R.backup=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Sauvegarde"));
  const box=$("div","send");
  rec.txt=$("p","mu small");rec.txt.style.margin="14px 0 0";
  rec.btn=$("button","btn ghost","Créer une sauvegarde maintenant");rec.msg=$("div","dmsg");rec.msg.style.fontSize="13px";rec.err=$("div","ferr");
  box.append(rec.txt,rec.btn,rec.msg,rec.err);rec.el.append(box);
  rec.btn.onclick=async()=>{rec.err.textContent="";rec.btn.disabled=true;
   const j=await post("api/backup",{});rec.btn.disabled=false;
   if(j.ok){backupNote="Sauvegarde créée : "+j.backup;}else rec.err.textContent=j.error||"Erreur.";};
 }
 rec.txt.textContent="La sauvegarde est une copie du portefeuille, créée par le nœud dans son dossier "+d.dir+" sur ton Raspberry Pi."
  +(d.encrypted?" Elle est protégée par ton mot de passe.":" Elle n'est PAS protégée par un mot de passe : garde-la privée.")
  +" Copie-la ensuite ailleurs que sur le Pi (carte SD, clé USB) : si le Pi tombe en panne, c'est ta seule chance de retrouver tes RDT. Refais-en une de temps en temps.";
 if(rec.shown!==backupNote){rec.msg.textContent=backupNote;rec.shown=backupNote;}
};

/* ---- envoyer */
R.send=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Envoyer des RDT"));
  const box=$("div","send");
  /* formulaire */
  rec.form=$("div","");
  rec.addr=$("input");rec.addr.type="text";rec.addr.placeholder="rdt1…";rec.addr.autocomplete="off";rec.addr.spellcheck=false;rec.addr.autocapitalize="off";
  rec.amt=$("input");rec.amt.type="text";rec.amt.inputMode="decimal";rec.amt.placeholder="0";rec.amt.autocomplete="off";
  const lc=$("label","chk");rec.all=$("input");rec.all.type="checkbox";lc.append(rec.all," Envoyer tout le solde disponible (les frais sont déduits)");
  rec.bal=$("div","mu small");rec.wait=$("div","warn small");rec.err=$("div","ferr");
  rec.go=$("button","btn","Vérifier avant d'envoyer");
  rec.form.append($("label","","Adresse du destinataire"),rec.addr,$("label","","Montant (RDT)"),rec.amt,lc,rec.bal,rec.wait,rec.go,rec.err);
  /* apercu */
  rec.prev=$("div","");rec.prev.hidden=true;
  rec.prows=$("div","prows");rec.pw=$("input");rec.pw.type="password";rec.pw.autocomplete="off";rec.pw.placeholder="Mot de passe du portefeuille";
  rec.pwl=$("label","","Mot de passe du portefeuille");
  rec.ok=$("button","btn","Confirmer l'envoi");rec.back=$("button","btn ghost","Modifier");rec.perr=$("div","ferr");
  const prow=$("div","btnrow");prow.append(rec.ok,rec.back);
  rec.prev.append($("div","ptitle","Vérifie bien avant de confirmer"),rec.prows,
   $("div","warn small","Un envoi ne peut pas être annulé. Contrôle l'adresse caractère par caractère."),rec.pwl,rec.pw,prow,rec.perr);
  /* resultat */
  rec.done=$("div","");rec.done.hidden=true;
  rec.dmsg=$("div","dmsg");rec.dtx=$("code","addr");rec.dnew=$("button","btn ghost","Faire un autre envoi");
  rec.done.append(rec.dmsg,rec.dtx,rec.dnew);
  box.append(rec.form,rec.prev,rec.done);rec.el.append(box);
  rec.busy=false;rec.token=null;
  const show=w=>{rec.form.hidden=w!=="form";rec.prev.hidden=w!=="prev";rec.done.hidden=w!=="done";};
  rec.show=show;
  rec.all.onchange=()=>{rec.amt.disabled=rec.all.checked;if(rec.all.checked)rec.amt.value="";};
  const lock=b=>{rec.busy=b;rec.go.disabled=b||!rec.canSend;rec.ok.disabled=b;rec.back.disabled=b;};
  rec.lock=lock;
  rec.go.onclick=async()=>{
   rec.err.textContent="";lock(true);rec.go.textContent="Vérification…";
   const j=await post("api/preview",{address:rec.addr.value,amount:rec.amt.value,all:rec.all.checked});
   rec.go.textContent="Vérifier avant d'envoyer";lock(false);
   if(!j.ok){rec.err.textContent=j.error||"Erreur.";return;}
   rec.token=j.token;rec.prows.textContent="";
   const row=(a,b,cls)=>{const r=$("div","prow");r.append($("span","mu",a),$("b",cls||"",b));rec.prows.append(r);};
   row("Montant envoyé",j.amount+" RDT","big2");
   row("Frais du réseau (environ)",j.fee+" RDT");
   row("Total retiré du solde",j.total+" RDT");
   const ad=$("div","prow col");ad.append($("span","mu","Adresse du destinataire"),$("code","addr",j.address.replace(/(.{4})/g,"$1 ").trim()));rec.prows.append(ad);
   if(j.self){const w=$("div","warn small","Cette adresse appartient à ton propre portefeuille : tu t'envoies des RDT à toi-même.");rec.prows.append(w);}
   rec.pw.value="";rec.perr.textContent="";
   rec.pwl.hidden=rec.pw.hidden=!rec.encrypted;
   show("prev");if(rec.encrypted)rec.pw.focus();
  };
  rec.back.onclick=()=>{rec.token=null;rec.pw.value="";show("form");};
  const confirm=async()=>{
   if(rec.busy||!rec.token)return;
   rec.perr.textContent="";lock(true);rec.ok.textContent="Envoi…";
   const j=await post("api/send",{token:rec.token,passphrase:rec.pw.value});
   rec.ok.textContent="Confirmer l'envoi";lock(false);
   if(!j.ok){rec.perr.textContent=j.error||"Erreur.";
    if(/expiré|déjà servi/.test(j.error||"")){rec.token=null;rec.err.textContent=j.error;rec.pw.value="";show("form");}
    return;}
   rec.token=null;rec.pw.value="";rec.addr.value="";rec.amt.value="";rec.all.checked=false;rec.amt.disabled=false;
   rec.dmsg.textContent="Envoyé ✓  La transaction attend maintenant d'être incluse dans un prochain bloc.";
   rec.dtx.textContent=j.txid;show("done");poll();
  };
  rec.ok.onclick=confirm;rec.pw.addEventListener("keydown",e=>{if(e.key==="Enter")confirm();});
  rec.dnew.onclick=()=>show("form");
  show("form");
 }
 rec.encrypted=d.encrypted;rec.canSend=d.can_send;
 rec.bal.textContent="Solde disponible : "+d.spendable+" RDT";
 rec.wait.textContent=d.can_send?"":"Envoi bloqué : le nœud n'est pas encore à jour.";
 if(!rec.busy)rec.go.disabled=!d.can_send;
 if(rec.prev.hidden===false){rec.pwl.hidden=rec.pw.hidden=!d.encrypted;}
};

/* ---- messages */
function fmtDt(ts){return new Date(ts*1000).toLocaleString("fr-FR",{day:"numeric",month:"long",year:"numeric",hour:"2-digit",minute:"2-digit"});}
R.msgs=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Messages dans la chaîne"));
  const box=$("div","send");
  rec.busy=false;rec.token=null;rec.max=d.max;rec.map=new Map();
  const show=w=>{rec.form.hidden=w!=="form";rec.prev.hidden=w!=="prev";rec.done.hidden=w!=="done";};
  /* 1. écrire */
  rec.form=$("div","");
  rec.ta=$("textarea");rec.ta.rows=4;rec.ta.placeholder="Écris un court message…";rec.ta.autocomplete="off";
  rec.cnt=$("div","mu small");rec.wait=$("div","warn small");rec.err=$("div","ferr");
  rec.go=$("button","btn","Vérifier avant d'écrire");
  rec.form.append($("label","","Ton message"),rec.ta,rec.cnt,
   $("div","mu small","Un message est public, lisible par tout le monde, et ne peut plus être effacé ni modifié. Ne mets jamais d'information personnelle, de mot de passe ou d'adresse."),
   rec.wait,rec.go,rec.err);
  /* 2. aperçu */
  rec.prev=$("div","");
  rec.ptxt=$("div","mtxt mprev");rec.prows=$("div","prows");
  rec.pw=$("input");rec.pw.type="password";rec.pw.autocomplete="off";rec.pw.placeholder="Mot de passe du portefeuille";
  rec.pwl=$("label","","Mot de passe du portefeuille");
  rec.ok=$("button","btn","Écrire dans la chaîne");rec.back=$("button","btn ghost","Modifier");rec.perr=$("div","ferr");
  const r2=$("div","btnrow");r2.append(rec.ok,rec.back);
  rec.prev.append($("div","ptitle","Vérifie avant d'écrire"),rec.ptxt,rec.prows,
   $("div","warn small","Une fois écrit, ce message est public et définitif : personne ne peut l'effacer, toi non plus."),rec.pwl,rec.pw,r2,rec.perr);
  /* 3. fini */
  rec.done=$("div","");
  rec.dmsg=$("div","dmsg");rec.dtx=$("code","addr");
  rec.dnote=$("div","mu small","Il apparaît ci-dessous, « en attente » d'abord, puis daté dès qu'un bloc le contient.");
  rec.dnew=$("button","btn ghost","Écrire un autre message");
  rec.done.append(rec.dmsg,rec.dtx,rec.dnote,rec.dnew);
  box.append(rec.form,rec.prev,rec.done);rec.el.append(box);
  /* 4. le fil */
  rec.fh=$("div","ptitle","Messages trouvés dans la chaîne");
  rec.warn=$("div","mu small","Ces messages viennent de n'importe qui : tout le monde peut écrire dans la chaîne. Ils ne sont ni vérifiés ni modérés. Seul le texte est affiché.");
  rec.list=$("div","msgs");rec.status=$("div","mu small mstat");
  rec.el.append(rec.fh,rec.warn,rec.list,rec.status);
  const upd=()=>{const n=[...rec.ta.value].length;rec.cnt.textContent=n+" / "+rec.max+" caractères";rec.cnt.className="small "+(n>rec.max?"bad":"mu");
   rec.go.disabled=rec.busy||!rec.can||n===0||n>rec.max;};
  rec.upd=upd;rec.ta.addEventListener("input",upd);
  const lock=b=>{rec.busy=b;rec.ok.disabled=b;rec.back.disabled=b;upd();};
  rec.go.onclick=async()=>{
   rec.err.textContent="";lock(true);rec.go.textContent="Vérification…";
   const j=await post("api/msgpreview",{text:rec.ta.value});
   rec.go.textContent="Vérifier avant d'écrire";lock(false);
   if(!j.ok){rec.err.textContent=j.error||"Erreur.";return;}
   rec.token=j.token;rec.ptxt.textContent=j.text;rec.prows.textContent="";
   const row=(a,b,cls)=>{const r=$("div","prow");r.append($("span","mu",a),$("b",cls||"",b));rec.prows.append(r);};
   row("Taille",j.chars+" caractères ("+j.bytes+" octets)");
   row("Frais du réseau (environ)",j.fee+" RDT","big2");
   rec.pw.value="";rec.perr.textContent="";rec.pwl.hidden=rec.pw.hidden=!rec.encrypted;
   show("prev");if(rec.encrypted)rec.pw.focus();
  };
  rec.back.onclick=()=>{rec.token=null;rec.pw.value="";show("form");};
  const confirm=async()=>{
   if(rec.busy||!rec.token)return;
   rec.perr.textContent="";lock(true);rec.ok.textContent="Envoi…";
   const j=await post("api/msgsend",{token:rec.token,passphrase:rec.pw.value});
   rec.ok.textContent="Écrire dans la chaîne";lock(false);
   if(!j.ok){rec.perr.textContent=j.error||"Erreur.";
    if(/expiré|déjà servi/.test(j.error||"")){rec.token=null;rec.err.textContent=j.error;rec.pw.value="";show("form");}
    return;}
   rec.token=null;rec.pw.value="";rec.ta.value="";upd();
   rec.dmsg.textContent="Message envoyé ✓";rec.dtx.textContent=j.txid;show("done");poll();
  };
  rec.ok.onclick=confirm;rec.pw.addEventListener("keydown",e=>{if(e.key==="Enter")confirm();});
  rec.dnew.onclick=()=>show("form");
  show("form");upd();
 }
 rec.encrypted=d.encrypted;rec.can=d.can_send;
 rec.wait.textContent=d.can_send?"":"Écriture bloquée : le nœud n'est pas encore à jour.";
 rec.upd();
 if(rec.prev.hidden===false){rec.pwl.hidden=rec.pw.hidden=!d.encrypted;}
 /* le fil : d'abord les messages de cette page encore en attente, puis ceux des blocs */
 const pend=new Set(d.pending.map(p=>p.txid));
 const all=d.pending.map(p=>({txid:p.txid,text:p.text,mine:true,time:null,height:null,conf:p.conf}))
  .concat(d.feed.filter(m=>!pend.has(m.txid)));
 const seen=new Set(),els=[];
 all.forEach(m=>{
  let r=rec.map.get(m.txid);
  if(!r){r={el:$("div","msg"),top:$("div","mtop"),txt:$("div","mtxt"),meta:$("div","mmeta mu small"),dt:$("span","mdt"),bd:$("span","badge")};
   r.top.append(r.dt,r.bd);r.el.append(r.top,r.txt,r.meta);rec.map.set(m.txid,r);}
  r.txt.textContent=m.text;
  if(m.time){r.dt.textContent=fmtDt(m.time);r.bd.textContent=m.conf+" confirmation"+(m.conf>1?"s":"");r.bd.className="badge good";}
  else{r.dt.textContent=m.conf>0?"Confirmé, lecture en cours":"En attente d'un bloc";r.bd.textContent=m.conf>0?"confirmé":"en attente";r.bd.className="badge warn";}
  r.meta.textContent=(m.mine?"Écrit depuis ton portefeuille · ":"")+(m.height!=null?"bloc "+m.height+" · ":"")+m.txid.slice(0,10)+"…"+m.txid.slice(-6);
  r.meta.title=m.txid;
  seen.add(m.txid);els.push(r.el);
 });
 for(const [k,r] of rec.map)if(!seen.has(k)){r.el.remove();rec.map.delete(k);}
 if(!rec.empty)rec.empty=$("div","empty");
 rec.empty.remove();
 if(!els.length){rec.empty.textContent=d.ready?"Aucun message dans les derniers blocs lus.":"Lecture de la chaîne en cours…";rec.list.append(rec.empty);}
 else order(rec.list,els);
 if(d.ready)rec.status.textContent=d.from!=null?"Messages cherchés dans les blocs "+d.from+" à "+d.to+".":"";
 else rec.status.textContent="Lecture de la chaîne en cours… "+(d.progress!=null?Math.round(d.progress*100)+" %":"");
};

function render(state){
 const root=document.getElementById("root"),keep=new Set(),els=[];
 state.sections.forEach(d=>{
  keep.add(d.id);let rec=reg[d.id];
  if(!rec||rec.type!==d.type){if(rec&&rec.el)rec.el.remove();if(rec&&rec.timer)clearInterval(rec.timer);rec=reg[d.id]={type:d.type};}
  (R[d.type]||(()=>{}))(rec,d);if(rec.el)els.push(rec.el);
 });
 for(const k in reg)if(!keep.has(k)){if(reg[k].el)reg[k].el.remove();if(reg[k].timer)clearInterval(reg[k].timer);delete reg[k];}
order(root,els);
 for(const k in reg){const r=reg[k];if(r.scrollEnd){r.band.style.scrollBehavior=r.scrollEnd;r.band.scrollLeft=r.band.scrollWidth;r.scrollEnd=null;}}
}

function setPill(ok,txt){const p=document.getElementById("pill");p.className="pill "+(ok?"ok":"ko");document.getElementById("pilltx").textContent=txt;}
async function poll(){
 try{
  const r=await fetch("api/state",{cache:"no-store"});if(!r.ok)throw new Error(r.status);
  const s=await r.json();offset=s.now-Date.now()/1000;lastOk=Date.now();
  document.getElementById("sub").textContent=s.subtitle||"";
  if(s.view==="ckpool"){const m=s.sections.find(x=>x.type==="miner");setPill(m&&m.state==="on",m?m.label:"ckpool");}
  else setPill(s.ok,s.ok?"Nœud en marche":"Nœud injoignable");
  render(s);
 }catch(e){setPill(false,"Page injoignable, nouvel essai…");}
}
setInterval(()=>{document.querySelectorAll("[data-age]").forEach(e=>{e.textContent=ago(+e.dataset.age);});
 const u=document.getElementById("upd");if(lastOk)u.textContent="mis à jour il y a "+Math.max(0,Math.round((Date.now()-lastOk)/1000))+" s";},1000);
poll();setInterval(poll,3000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)poll();});
</script></body></html>
"""


def page():
    return (HTML.replace("__TITLE__", TITLES.get(VIEW, "Radthal")).replace("__VIEW__", VIEW)).encode("utf-8")


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            self._send(200, json.dumps(get_state(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send(200, page(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if VIEW != "wallet" or path not in ("/api/preview", "/api/send", "/api/newaddress", "/api/encrypt", "/api/backup",
                                         "/api/msgpreview", "/api/msgsend"):
            return self._send(404, b"not found", "text/plain")
        # une page d'un autre site ne doit pas pouvoir declencher un envoi : JSON obligatoire (ce qui force une
        # verification du navigateur que ce serveur ne l'autorise jamais) et refus des requetes inter-sites
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return self._json(403, {"ok": False, "error": "Requête refusée."})
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"ok": False, "error": "Requête refusée."})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 16384:
                raise ValueError
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self._json(400, {"ok": False, "error": "Requête illisible."})
        try:
            if path == "/api/preview":
                out = wallet_preview(data.get("address"), data.get("amount"), data.get("all") is True)
            elif path == "/api/send":
                out = {"txid": wallet_send(data.get("token"), data.get("passphrase", ""))}
            elif path == "/api/encrypt":
                out = wallet_encrypt(data.get("pass1"), data.get("pass2"), data.get("ack"))
            elif path == "/api/backup":
                out = {"backup": wallet_backup()}
            elif path == "/api/msgpreview":
                out = msg_preview(data.get("text"))
            elif path == "/api/msgsend":
                out = {"txid": msg_send(data.get("token"), data.get("passphrase", ""))}
            else:
                out = {"address": wallet_new_address()}
            self._json(200, dict(out, ok=True))
        except UserError as e:
            self._json(200, {"ok": False, "error": str(e)})
        except Exception:
            self._json(500, {"ok": False, "error": "Erreur interne. Réessaie."})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if VIEW != "wallet":
        threading.Thread(target=wallet_loop, daemon=True).start()
    else:
        threading.Thread(target=msg_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
