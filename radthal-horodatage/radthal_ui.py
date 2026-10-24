"""Interface web de l'application Umbrel Radthal Horodatage (prouver qu'un document existait a une date donnee).

Un seul fichier, aucune dependance (Python 3 seulement). Le serveur lit le noeud (RPC) et les
fichiers de ckpool, et sert :
  /           la page (HTML + CSS + JS, style anime comme la video de presentation)
  /api/state  les donnees de la vue (VIEW=stamp), en JSON
  /api/preview, /api/stamp, /api/verify  requetes POST en JSON
  /cert/<txid>.json, /registre.json      telechargements

La page interroge /api/state toutes les 3 secondes et met l'affichage a jour sans recharger.
"""
import base64, glob, json, math, os, re, secrets, threading, time, urllib.error, urllib.parse, urllib.request
from decimal import Decimal, ROUND_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
try:
    # identifiant de cette version du programme : change tout seul a chaque mise a jour du fichier
    UI_VER = hashlib.sha1(open(__file__, "rb").read()).hexdigest()[:12]
except Exception:
    UI_VER = "0"

VIEW = os.environ.get("VIEW", "dashboard")
RPC = os.environ.get("RPC", "http://radthal-host:9332")
RPCUSER = os.environ.get("RPCUSER", "radthal")
PASSFILE = os.environ.get("PASSFILE", "/run/secrets/radthal_rpc_password")
LOGS = os.environ.get("LOGS", "/ckpool/logs")
PORT = int(os.environ.get("PORT", "8000"))
WALLET = os.environ.get("WALLET", "minage")
STRATUM = os.environ.get("STRATUM_PORT", "3333")
TITLES = {"node": "Nœud Radthal", "dashboard": "Tableau de bord Radthal",
          "mempool": "Mempool Radthal", "ckpool": "ckpool Radthal", "stamp": "Horodatage Radthal"}
SUBTITLES = {"node": "L'état de ton nœud complet",
             "dashboard": "La chaîne Radthal en direct",
             "mempool": "Les transactions en attente d'un bloc",
             "ckpool": "Ton mineur et ton pool solo",
             "stamp": "Prouve qu'un document existait à une date précise"}


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


def rpc_batch(calls, wallet=None, timeout=60):
    """Plusieurs appels RPC dans UNE requete HTTP. Renvoie la liste des resultats (None = cet appel a echoue),
    ou None si la requete entiere echoue."""
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        url = RPC + ("/wallet/" + wallet if wallet else "")
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


_W = {"mined": {}, "light": False, "scanned": False, "retry_at": 0.0}
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


def chain_scan(tip, step=200):
    """Blocs de la meilleure chaine dont la recompense va a ce portefeuille.
    On lit la CHAINE par paquets de `step` blocs (petites requetes groupees) au lieu de demander tout le portefeuille
    au noeud : listtransactions sur un portefeuille de centaines de milliers de transactions fait gonfler le noeud
    de plusieurs Go. Ici, la memoire du noeud reste petite quelle que soit la taille du portefeuille."""
    found, known = {}, {}
    for lo in range(1, tip + 1, step):                 # le bloc 0 (genesis) n'a pas de transaction lisible
        hs = list(range(lo, min(lo + step, tip + 1)))
        hashes = rpc_batch([("getblockhash", [h]) for h in hs])
        if not hashes or None in hashes:
            return None
        blocks = rpc_batch([("getblock", [bh, 1]) for bh in hashes])
        if not blocks or None in blocks:
            return None
        cbs = rpc_batch([("getrawtransaction", [b["tx"][0], True, b["hash"]]) for b in blocks])
        if not cbs or None in cbs:
            return None
        todo = []
        for cb in cbs:
            for o in cb.get("vout", []):
                a = (o.get("scriptPubKey") or {}).get("address")
                if a and a not in known and a not in todo:
                    todo.append(a)
        if todo:
            res = rpc_batch([("getaddressinfo", [a]) for a in todo], wallet=WALLET)
            if res is None or None in res:              # portefeuille absent ou noeud occupe : on recommencera plus tard
                return None
            for a, r in zip(todo, res):
                known[a] = bool(r and r.get("ismine"))
        for h, bh, cb in zip(hs, hashes, cbs):
            if any(known.get((o.get("scriptPubKey") or {}).get("address")) for o in cb.get("vout", [])):
                found[bh] = {"blockhash": bh, "amount": None, "confirmations": tip - h + 1, "blockheight": h}
        time.sleep(0.02)                                # laisse respirer le noeud
    return found


def wallet_refresh():
    """1) les 200 dernieres transactions (rapide) : sert a marquer tes blocs ;
    2) une seule fois, la chaine entiere par petits paquets (voir chain_scan) pour compter tous tes blocs."""
    txs = rpc("listtransactions", ["*", 200, 0], wallet=WALLET)
    if txs is None:
        return
    with _W_LOCK:
        _wallet_apply(txs, _W["mined"])
        _W["light"] = True
        if not FULL_SCAN:
            _W["scanned"] = True
    if FULL_SCAN and not _W["scanned"] and time.time() >= _W["retry_at"]:
        info = rpc("getblockchaininfo")
        try:
            mined = chain_scan(info["blocks"]) if info and info.get("blocks") else None
        except Exception:
            mined = None
        if mined is None:
            _W["retry_at"] = time.time() + 60            # on reessaiera dans une minute, sans harceler le noeud
            return
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
        return card("found", "Blocs trouvés", value="calcul…", sub="lecture de la chaîne (une fois, ça peut prendre une minute)")
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


# ---------------------------------------------------------------- horodatage
# Un horodatage = une transaction qui porte, dans une sortie OP_RETURN de 36 octets, "RDTS" suivi de l'empreinte
# SHA-256 du document (jamais le document lui-meme). La date prouvee est celle du bloc qui contient la
# transaction. La liste des horodatages est gardee dans STATE_DIR/horodatages.json (ecrite par cette page).
# Le mot de passe du portefeuille est demande a chaque horodatage : il deverrouille le portefeuille 30 secondes
# au plus, puis le portefeuille est reverrouille tout de suite. Il n'est ni garde ni ecrit nulle part.

STATE_DIR = os.environ.get("STATE_DIR", "/state")                    # dossier de la liste, vu par la page
STATE_SHOW = os.environ.get("STATE_SHOW", STATE_DIR)                 # le meme dossier, tel qu'on le voit sur le Pi
REG_FILE = os.path.join(STATE_DIR, "horodatages.json")
MAGIC_HEX = b"RDTS".hex()
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
TX_VB = 170                 # taille habituelle d'une transaction d'horodatage (octets virtuels)
SHOWN = 100                 # nombre d'horodatages affiches
SAT = Decimal("0.00000001")
HEX64 = re.compile(r"[0-9a-f]{64}")
_WLOCK = threading.Lock()      # un seul horodatage a la fois
_PREV = {}                     # jeton d'apercu -> {hash, name, size, rate, t}
_FAILS = []                    # instants des mots de passe refuses (limite : 5 en 10 minutes)
_REG = {"items": None, "broken": False}
_REG_LOCK = threading.Lock()
_ST = {}                       # txid -> (instant, etat) : evite de redemander au noeud a chaque rafraichissement


class UserError(Exception):
    """Message destine a la personne qui utilise la page."""


def rpc_full(method, params=None, wallet=None, timeout=20):
    """Appel RPC qui renvoie (resultat, erreur). Les montants restent exacts (Decimal).
    params peut etre une liste ou un dictionnaire (parametres nommes)."""
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        url = RPC + ("/wallet/" + urllib.parse.quote(wallet, safe="") if wallet else "")
        body = json.dumps({"jsonrpc": "1.0", "id": "h", "method": method,
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
    if code == -6 or "insufficient funds" in low or "exceeds your balance" in low:
        return ("Solde disponible insuffisant pour payer les frais. Les récompenses de minage ne sont "
                "utilisables qu'après 100 blocs.")
    if "scriptpubkey" in low or "datacarrier" in low or "nulldata" in low or "op_return" in low:
        return ("Ton nœud refuse les transactions qui portent une donnée (OP_RETURN). Vérifie que l'option "
                "datacarrier n'est pas désactivée dans la configuration du nœud.")
    if "fee" in low and ("not met" in low or "too low" in low or "insufficient" in low):
        return "Les frais sont trop bas pour ton nœud en ce moment. Réessaie dans un instant."
    if "too large" in low:
        return "La transaction serait trop volumineuse (trop de petites pièces à regrouper)."
    if "fee estimation failed" in low:
        return "Le nœud n'a pas pu estimer les frais."
    return "Refusé par le nœud : " + msg


def plain(d):
    """Montant en texte pour le noeud : 12.5 (point, jamais d'exposant)."""
    s = format(Decimal(d).quantize(SAT), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_rdt(d):
    """Montant pour l'affichage : 1 234,5 (espaces, virgule), au moins 2 decimales."""
    ip, _, fp = plain(d).partition(".")
    return "{:,}".format(int(ip)).replace(",", " ") + ("," + fp if fp else "")


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


def iso_utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def check_hash(h):
    h = str(h or "").strip().lower()
    if not HEX64.fullmatch(h):
        raise UserError("Empreinte invalide.")
    return h


def clean_name(s, default="Document"):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()[:120]
    return s or default


# ---- la liste des horodatages (fichier JSON)

def _reg_load_locked():
    if _REG["items"] is None:
        items = []
        try:
            with open(REG_FILE, encoding="utf-8") as f:
                d = json.load(f)
            for x in (d.get("items") if isinstance(d, dict) else None) or []:
                if isinstance(x, dict) and HEX64.fullmatch(str(x.get("hash", ""))) and HEX64.fullmatch(str(x.get("txid", ""))):
                    size = x.get("size")
                    vout = x.get("vout")
                    items.append({"hash": x["hash"], "txid": x["txid"],
                                  "vout": vout if isinstance(vout, int) and not isinstance(vout, bool) else None,
                                  "name": clean_name(x.get("name")),
                                  "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
                                  "t": float(x.get("t") or 0), "fee": str(x.get("fee") or "")})
        except FileNotFoundError:
            pass
        except Exception:
            _REG["broken"] = True          # fichier illisible : on ne l'ecrasera pas, on le met de cote
        _REG["items"] = items
    return _REG["items"]


def reg_items():
    with _REG_LOCK:
        return list(_reg_load_locked())


def reg_check_writable():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        p = os.path.join(STATE_DIR, ".test-ecriture")
        with open(p, "w") as f:
            f.write("ok")
        os.remove(p)
    except OSError:
        raise UserError("Impossible d'écrire la liste des horodatages dans %s. Rien n'a été envoyé." % STATE_SHOW)


def reg_add(entry):
    with _REG_LOCK:
        items = _reg_load_locked() + [entry]
        os.makedirs(STATE_DIR, exist_ok=True)
        if _REG["broken"] and os.path.exists(REG_FILE):
            os.replace(REG_FILE, REG_FILE + ".illisible-" + time.strftime("%Y%m%d-%H%M%S"))
            _REG["broken"] = False
        tmp = REG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"format": "radthal-horodatages", "version": 1, "items": items}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REG_FILE)
        _REG["items"] = items


def reg_export():
    return json.dumps({"format": "radthal-horodatages", "version": 1, "items": reg_items()},
                      ensure_ascii=False, indent=1).encode("utf-8")


# ---- etat d'une transaction

def tx_state(txid, fresh=False):
    now = time.time()
    c = _ST.get(txid)
    if c and not fresh and now - c[0] < (300 if c[1].get("conf", 0) >= 6 else 5):
        return c[1]
    r, e = rpc_full("gettransaction", [txid, True, False], wallet=WALLET)
    if e:
        # -5 : le portefeuille ne connait pas cette transaction. Toute autre erreur (noeud arrete, portefeuille
        # decharge) ne dit rien sur la transaction : on ne la declare pas perdue.
        st = {"state": "lost" if e.get("code") == -5 else "unknown", "conf": 0}
        if st["state"] == "unknown":
            return st
    else:
        conf = int(r.get("confirmations", 0))
        if conf < 0:
            st = {"state": "rejected", "conf": conf}
        elif conf == 0:
            _, me = rpc_full("getmempoolentry", [txid])
            st = {"state": "pending", "conf": 0, "inpool": me is None}
        else:
            st = {"state": "ok", "conf": conf, "blockhash": r.get("blockhash"),
                  "height": r.get("blockheight"), "blocktime": r.get("blocktime")}
    _ST[txid] = (now, st)
    return st


def find_live(h):
    """Un horodatage de ce document dont la transaction existe encore (en attente ou confirmee)."""
    for x in reg_items():
        if x["hash"] == h and tx_state(x["txid"])["state"] in ("ok", "pending", "unknown"):
            return x
    return None


# ---- horodater

def synced_ok():
    info, _ = rpc_full("getblockchaininfo")
    if not info:
        raise UserError("Le nœud ne répond pas. Vérifie qu'il est démarré dans Umbrel.")
    blocks, headers = info.get("blocks", 0), info.get("headers", 0)
    tip_t = info.get("time") or info.get("mediantime")
    recent = bool(tip_t) and time.time() - tip_t < 3600
    if not (headers > 0 and blocks >= headers and (not info.get("initialblockdownload") or recent)):
        raise UserError("Le nœud n'est pas encore à jour : attends la fin de la synchronisation.")


def stamp_preview(h, name, size):
    h = check_hash(h)
    if h == EMPTY_SHA:
        raise UserError("Ce document est vide : il n'y a rien à horodater.")
    name = clean_name(name)
    size = size if isinstance(size, int) and not isinstance(size, bool) and 0 <= size < 2 ** 50 else None
    if find_live(h):
        raise UserError("Ce document a déjà été horodaté. Retrouve-le dans « Mes horodatages » plus bas, "
                        "ou utilise « Vérifier un document ».")
    synced_ok()
    reg_check_writable()
    rate = fee_rate()
    res, e = rpc_full("walletcreatefundedpsbt",
                      [[], [{"data": MAGIC_HEX + h}], 0, {"fee_rate": plain(rate), "replaceable": False}],
                      wallet=WALLET)
    if e:
        raise UserError(human_error(e))
    fee = Decimal(res["fee"])
    now = time.time()
    for k in [k for k, p in _PREV.items() if now - p["t"] > 300]:
        _PREV.pop(k, None)
    token = secrets.token_hex(16)
    _PREV[token] = {"hash": h, "name": name, "size": size, "rate": plain(rate), "t": now}
    return {"token": token, "hash": h, "name": name, "fee": fmt_rdt(fee)}


def find_out(vouts, payload):
    """Numero de la sortie OP_RETURN qui porte exactement la donnee voulue, sinon None."""
    for vo in vouts or []:
        spk = vo.get("scriptPubKey") or {}
        if spk.get("type") == "nulldata":
            parts = str(spk.get("asm", "")).split()
            if len(parts) == 2 and parts[0] == "OP_RETURN" and parts[1].lower() == payload:
                return int(vo.get("n", 0))
    return None


def _broadcast(p):
    """Cree, signe, controle puis diffuse la transaction. Le portefeuille doit etre deverrouille.
    La transaction n'est confiee au portefeuille qu'apres avoir ete acceptee par le noeud (testmempoolaccept)."""
    payload = MAGIC_HEX + p["hash"]
    r, e = rpc_full("walletcreatefundedpsbt",
                    [[], [{"data": payload}], 0, {"fee_rate": p["rate"], "replaceable": False}], wallet=WALLET)
    if e:
        raise UserError(human_error(e))
    fee = Decimal(r["fee"])
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
    dec, _ = rpc_full("decoderawtransaction", [raw])
    return str(txid), find_out((dec or {}).get("vout"), payload), fee


def stamp_send(token, passphrase):
    now = time.time()
    token = str(token or "")
    if not isinstance(passphrase, str) or len(passphrase) > 1024:
        raise UserError("Mot de passe invalide.")
    warn = None
    with _WLOCK:
        p = _PREV.get(token)
        if not p or now - p["t"] > 300:
            _PREV.pop(token, None)
            raise UserError("Cet aperçu a expiré ou a déjà servi. Recommence l'horodatage depuis le début.")
        if find_live(p["hash"]):
            _PREV.pop(token, None)
            raise UserError("Ce document vient d'être horodaté. Il n'est pas nécessaire de recommencer.")
        reg_check_writable()
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
            txid, vout, fee = _broadcast(p)
        finally:
            if unlocked:
                rpc_full("walletlock", wallet=WALLET)
        entry = {"hash": p["hash"], "txid": txid, "vout": vout, "name": p["name"], "size": p["size"],
                 "t": time.time(), "fee": plain(fee)}
        try:
            reg_add(entry)
        except OSError:
            warn = ("L'horodatage est bien envoyé, mais la liste n'a pas pu être mise à jour. Note l'identifiant "
                    "de la transaction ci-dessous : il suffit à le retrouver.")
    _cache["t"] = 0
    return {"txid": txid, "hash": p["hash"], "warn": warn}


# ---- verifier, certificat

def fetch_tx(txid, blockhash=None):
    """La transaction vue par le noeud : {vout, conf, blockhash, height, blocktime, mediantime} ou None."""
    params = [txid, True] + ([blockhash] if blockhash else [])
    r, e = rpc_full("getrawtransaction", params)
    if e and e.get("code") == -999:
        raise UserError(human_error(e))
    if not e:
        out = {"vout": r.get("vout", []), "conf": int(r.get("confirmations", 0)), "blockhash": r.get("blockhash")}
        if blockhash and r.get("in_active_chain") is False:
            out.update(conf=0, blockhash=None, stale=True)
    else:
        r, e = rpc_full("gettransaction", [txid, True, True], wallet=WALLET)
        if e:
            return None
        out = {"vout": (r.get("decoded") or {}).get("vout", []), "conf": int(r.get("confirmations", 0)),
               "blockhash": r.get("blockhash")}
    if out["conf"] > 0 and out.get("blockhash"):
        hd, e = rpc_full("getblockheader", [out["blockhash"]])
        if not e and hd:
            out.update(height=hd.get("height"), blocktime=hd.get("time"), mediantime=hd.get("mediantime"))
    return out


def verify_tx(h, txid, blockhash=None):
    tx = fetch_tx(txid, blockhash)
    if tx is None:
        return {"status": "notfound"}
    n = find_out(tx["vout"], MAGIC_HEX + h)
    if n is None:
        return {"status": "mismatch"}
    if tx.get("stale"):
        return {"status": "stale"}
    out = {"txid": txid, "vout": n, "conf": tx["conf"]}
    if tx["conf"] <= 0:
        out["status"] = "pending"
    else:
        out.update(status="ok", height=tx.get("height"), blocktime=tx.get("blocktime"), blockhash=tx.get("blockhash"))
    return out


def verify(h, txid=None, blockhash=None, genesis=None):
    h = check_hash(h)
    if txid is None:
        found = sorted((x for x in reg_items() if x["hash"] == h), key=lambda x: x["t"])
        if not found:
            return {"status": "none"}
        res = [verify_tx(h, x["txid"]) for x in found]
        for want in ("ok", "pending"):
            for r in res:
                if r["status"] == want:
                    return r
        return res[0]
    txid = str(txid).strip().lower()
    if not HEX64.fullmatch(txid):
        raise UserError("Le certificat contient un identifiant de transaction invalide.")
    if blockhash is not None:
        blockhash = str(blockhash).strip().lower()
        if not HEX64.fullmatch(blockhash):
            raise UserError("Le certificat contient un identifiant de bloc invalide.")
    if genesis is not None:
        gen, _ = rpc_full("getblockhash", [0])
        if gen and str(genesis).strip().lower() != gen:
            return {"status": "other-chain"}
    return verify_tx(h, txid, blockhash)


def certificate(txid):
    if not HEX64.fullmatch(str(txid)):
        raise UserError("Identifiant de transaction invalide.")
    ent = next((x for x in reg_items() if x["txid"] == txid), None)
    if not ent:
        raise UserError("Horodatage inconnu.")
    st = tx_state(txid, fresh=True)
    if st["state"] != "ok":
        raise UserError("Le certificat est disponible après la première confirmation, c'est-à-dire quand "
                        "un bloc contenant l'horodatage a été trouvé.")
    tx = fetch_tx(txid, st.get("blockhash"))
    if not tx or tx.get("blocktime") is None:
        raise UserError("Le nœud n'a pas pu fournir les informations du bloc. Réessaie dans un instant.")
    payload = MAGIC_HEX + ent["hash"]
    n = find_out(tx["vout"], payload)
    if n is None:
        raise UserError("La transaction ne contient pas l'empreinte attendue.")
    gen, _ = rpc_full("getblockhash", [0])
    return {
        "format": "radthal-timestamp", "version": 1,
        "about": "Certificat d'horodatage Radthal : l'empreinte du document a été écrite dans la chaîne Radthal, "
                 "dans le bloc indiqué. Le document lui-même n'est pas dans la chaîne.",
        "chain": "Radthal", "genesis": gen,
        "algorithm": "SHA-256", "hash": ent["hash"], "payload": payload,
        "file_name": ent["name"], "file_size": ent["size"],
        "txid": txid, "vout": n,
        "block": {"hash": tx["blockhash"], "height": tx.get("height"), "time": tx["blocktime"],
                  "time_utc": iso_utc(tx["blocktime"]), "mediantime": tx.get("mediantime")},
        "confirmations_when_created": tx["conf"], "created_utc": iso_utc(time.time()),
        "how_to_verify": [
            "1. Calcule l'empreinte SHA-256 du document (Linux ou Mac : sha256sum fichier ; Windows : certutil -hashfile fichier SHA256). "
            "Elle doit être identique à « hash ».",
            "2. Sur un nœud Radthal à jour, demande la transaction : getrawtransaction <txid> true <block.hash>.",
            "3. Dans la réponse, une sortie OP_RETURN doit contenir exactement « payload » (RDTS suivi de l'empreinte).",
            "4. Le bloc donne la date : le document existait au plus tard à block.time_utc. L'heure d'un bloc est "
            "fixée par son mineur et peut s'écarter de l'heure réelle de quelques heures."],
    }


# ---- la page

def view_stamp():
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
    encrypted = "unlocked_until" in wi
    items = reg_items()
    rows, n_ok, n_wait = [], 0, 0
    for x in list(reversed(items))[:SHOWN]:
        st = tx_state(x["txid"])
        n_ok += st["state"] == "ok"
        n_wait += st["state"] == "pending"
        rows.append({"txid": x["txid"], "hash": x["hash"], "name": x["name"], "size": x["size"], "sent": x["t"],
                     "state": st["state"], "conf": st.get("conf", 0), "blocktime": st.get("blocktime"),
                     "height": st.get("height"), "inpool": st.get("inpool", True)})
    fee = fee_rate() * TX_VB * SAT
    secs.append(cards("stat", [
        card("total", "Documents horodatés", num=len(items), accent=True),
        card("wait", "En attente d'un bloc", num=n_wait, sub="Daté dès qu'un bloc est trouvé"),
        card("fee", "Frais par horodatage", num=float(fee), dec=max(2, min(8, len(plain(fee).partition(".")[2]))),
             unit="RDT", sub="Quelques octets seulement dans la chaîne"),
    ]))
    if not synced:
        secs.append(note("sync", "Le nœud n'est pas encore à jour (%s blocs sur %s) : l'horodatage est bloqué "
                                 "et la vérification peut être incomplète." % (blocks, headers), "warn"))
    if not encrypted:
        secs.append(note("unenc", "Ton portefeuille n'est pas protégé par un mot de passe : l'horodatage se fait "
                                  "sans mot de passe. Tu peux le protéger depuis l'application Radthal Wallet."))
    secs.append({"id": "make", "type": "make", "encrypted": encrypted, "can_stamp": synced})
    secs.append({"id": "check", "type": "check"})
    secs.append({"id": "stamps", "type": "stamps", "rows": rows, "total": len(items), "shown": SHOWN,
                 "dir": STATE_SHOW})
    secs.append({"id": "about", "type": "about"})
    return secs, info


VIEWS = {"node": view_node, "dashboard": view_dashboard, "mempool": view_mempool, "ckpool": view_ckpool,
         "stamp": view_stamp}

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
        if _cache["data"] is None or time.time() - _cache["t"] > (6.0 if VIEW == "stamp" else 2.0):
            _cache["data"] = build_state()
            _cache["t"] = time.time()
        return _cache["data"]


# ---------------------------------------------------------------- page

HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>__TITLE__</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink' width='256' height='256' viewBox='0 0 256 256'%3E%3Cdefs%3E%3ClinearGradient id='rimk' x1='0.1' y1='0' x2='0.9' y2='1'%3E%3Cstop offset='0' stop-color='%230055A4'/%3E%3Cstop offset='0.3' stop-color='%230055A4'/%3E%3Cstop offset='0.65' stop-color='%230055A4'/%3E%3Cstop offset='1' stop-color='%230055A4'/%3E%3C/linearGradient%3E%3CradialGradient id='facek' cx='50%25' cy='42%25' r='70%25'%3E%3Cstop offset='0' stop-color='%23050b17'/%3E%3Cstop offset='0.55' stop-color='%23050b17'/%3E%3Cstop offset='1' stop-color='%23050b17'/%3E%3C/radialGradient%3E%3ClinearGradient id='lgk' gradientUnits='userSpaceOnUse' x1='0' y1='62' x2='0' y2='194'%3E%3Cstop offset='0' stop-color='%230055A4'/%3E%3Cstop offset='0.35' stop-color='%230055A4'/%3E%3Cstop offset='0.7' stop-color='%230055A4'/%3E%3Cstop offset='1' stop-color='%230055A4'/%3E%3C/linearGradient%3E%3CradialGradient id='shinek' cx='30%25' cy='22%25' r='55%25'%3E%3Cstop offset='0' stop-color='%23fff' stop-opacity='0.28'/%3E%3Cstop offset='1' stop-color='%23fff' stop-opacity='0'/%3E%3C/radialGradient%3E%3Cfilter id='shk' x='-20%25' y='-20%25' width='140%25' height='140%25'%3E%3CfeGaussianBlur stdDeviation='3.2'/%3E%3C/filter%3E%3CclipPath id='clipk'%3E%3Ccircle cx='128' cy='128' r='106'/%3E%3C/clipPath%3E%3Cpath id='R' d='M63 65.61 79.18 67.05C79.54 85.57 79.54 104.09 79.54 122.79V130.7C79.54 149.4 79.54 167.92 79.18 186.08L63 187.7V193.81H126.65V187.7L110.11 186.08C109.57 167.74 109.57 149.22 109.57 130.34H118.2C132.05 130.34 136.36 137.53 138.88 152.27L144.09 177.09C145.89 191.65 155.06 196.51 171.6 196.51C180.59 196.51 185.63 195.61 193 193.81V187.7L177 186.62L171.42 157.13C168.37 139.33 161.89 129.26 138.34 126.2C166.75 121.89 176.64 108.04 176.64 93.3C176.64 71.9 159.38 59.49 127.91 59.49H63ZM110.11 66.33H120.18C138.34 66.33 147.87 76.04 147.87 93.84C147.87 110.2 140.14 123.68 120 123.68H109.57C109.57 103.01 109.57 84.49 110.11 66.33Z'/%3E%3C/defs%3E%3Ccircle cx='128' cy='128' r='124' fill='url(%23rimk)'/%3E%3Ccircle cx='128' cy='128' r='108.2' fill='%2302122a'/%3E%3Ccircle cx='128' cy='128' r='106' fill='url(%23facek)'/%3E%3Cuse xlink:href='%23R' transform='translate(0 4)' fill='%23000' fill-opacity='0.6' filter='url(%23shk)'/%3E%3Cuse xlink:href='%23R' transform='translate(1.6 2.2)' fill='%23003a75'/%3E%3Cuse xlink:href='%23R' fill='url(%23lgk)'/%3E%3C/svg%3E">
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
.brand svg{width:42px;height:42px;flex:none}
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
/* horodatage */
[hidden]{display:none!important}
.small{font-size:12px;line-height:1.45}
@media(max-width:480px){.cards{grid-template-columns:1fr}}
.addr{display:block;background:#0e1626;border:1px solid var(--edge);border-radius:8px;padding:10px 12px;margin:6px 0 10px;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
.addr.hx{word-break:normal;overflow-wrap:anywhere}
.btnrow{display:flex;gap:8px;flex-wrap:wrap}
.send{background:var(--card);border:1px solid var(--edge);border-radius:16px;padding:6px 20px 20px}
.send label{display:block;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--mu);margin:14px 0 6px}
.send input[type=text],.send input[type=password],.send textarea{width:100%;background:#0e1626;border:1px solid var(--edge);border-radius:10px;color:var(--tx);padding:12px;font:15px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.send textarea{font-size:14px;line-height:1.5;resize:vertical}
.send input:focus,.send textarea:focus{outline:none;border-color:var(--acc)}
.send input[type=file]{width:100%;margin-top:6px;color:var(--mu);font-size:13px}
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
.prow b.nm2{overflow-wrap:anywhere;text-align:right}
.dmsg{color:var(--good);font-weight:700;margin:14px 0 4px}
details.prot{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:4px 18px 16px;margin-top:14px}
details.prot summary{cursor:pointer;font-weight:700;font-size:14px;padding:12px 0 6px}
details.prot p{margin:8px 0;font-size:13px;line-height:1.5;color:var(--mu)}
details.prot b.tx{color:var(--tx)}
details.prot textarea{margin:6px 0}
.dz{border:2px dashed var(--edge);border-radius:14px;padding:28px 16px;text-align:center;cursor:pointer;background:#0e1626;margin-top:14px;transition:border-color .2s,background .2s}
.dz:hover,.dz:focus-visible,.dz.over{border-color:var(--acc);background:#1a1410;outline:none}
.dzt{font-size:16px;font-weight:700;margin-bottom:6px}
.pg{height:10px;border-radius:5px;background:#0e1626;border:1px solid var(--edge);overflow:hidden;margin-top:14px}
.pg i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .15s}
.pgm{margin-top:6px}
.res{border:1px solid var(--edge);border-radius:12px;padding:14px 16px;margin-top:16px;background:#0e1626;font-size:14px}
.res .rt{font-size:17px;font-weight:800;margin-bottom:6px}
.res .rl{margin-top:5px;line-height:1.45;overflow-wrap:anywhere}
.res.rg{border-color:var(--good)}.res.rg .rt{color:var(--good)}
.res.rw{border-color:var(--warn)}.res.rw .rt{color:var(--warn)}
.res.rb{border-color:var(--bad)}.res.rb .rt{color:var(--bad)}
.stl{background:var(--card);border:1px solid var(--edge);border-radius:14px}
.st{display:grid;grid-template-columns:minmax(150px,1fr) minmax(0,2fr) auto;gap:8px 16px;align-items:center;padding:13px 16px;border-bottom:1px solid rgba(46,58,80,.55)}
.st:last-child{border-bottom:0}
@media(max-width:700px){.st{grid-template-columns:1fr}}
.st .dt{font-weight:700;font-size:14px;margin-bottom:5px}
.st .nm{font-size:14px;overflow-wrap:anywhere}
.st .hh{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--mu);margin-top:3px}
.st .act{display:flex;gap:8px;flex-wrap:wrap}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:11px;font-weight:700;border:1px solid var(--edge);color:var(--mu)}
.badge.good{border-color:var(--good);color:var(--good)}.badge.warn{border-color:var(--warn);color:var(--warn)}.badge.bad{border-color:var(--bad);color:var(--bad)}
a.cp{display:inline-block;text-decoration:none;background:#0e1626;color:var(--tx);border:1px solid var(--edge);border-radius:8px;padding:6px 12px;font:600 12px Inter,system-ui,sans-serif;cursor:pointer}
a.cp:hover{border-color:var(--acc);color:var(--acc2)}
.stfoot{margin-top:10px}
footer{margin-top:34px;color:#6b7890;font-size:12px;text-align:center}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style></head><body><div class="wrap">
<header class="top">
 <div class="brand">
  <svg viewBox="0 0 256 256" aria-hidden="true"><defs><linearGradient id="rdc-rimk" x1="0.1" y1="0" x2="0.9" y2="1"><stop offset="0" stop-color="#0055A4"/><stop offset="0.3" stop-color="#0055A4"/><stop offset="0.65" stop-color="#0055A4"/><stop offset="1" stop-color="#0055A4"/></linearGradient><radialGradient id="rdc-facek" cx="50%" cy="42%" r="70%"><stop offset="0" stop-color="#050b17"/><stop offset="0.55" stop-color="#050b17"/><stop offset="1" stop-color="#050b17"/></radialGradient><linearGradient id="rdc-lgk" gradientUnits="userSpaceOnUse" x1="0" y1="62" x2="0" y2="194"><stop offset="0" stop-color="#0055A4"/><stop offset="0.35" stop-color="#0055A4"/><stop offset="0.7" stop-color="#0055A4"/><stop offset="1" stop-color="#0055A4"/></linearGradient><radialGradient id="rdc-shinek" cx="30%" cy="22%" r="55%"><stop offset="0" stop-color="#fff" stop-opacity="0.28"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient><filter id="rdc-shk" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="3.2"/></filter><clipPath id="rdc-clipk"><circle cx="128" cy="128" r="106"/></clipPath><path id="rdc-R" d="M75 77.13 88.2 78.3C88.49 93.4 88.49 108.5 88.49 123.75V130.2C88.49 145.45 88.49 160.55 88.2 175.36L75 176.67V181.66H126.9V176.67L113.41 175.36C112.97 160.4 112.97 145.3 112.97 129.91H120.01C131.3 129.91 134.82 135.77 136.87 147.79L141.12 168.02C142.59 179.9 150.07 183.86 163.55 183.86C170.88 183.86 174.99 183.13 181 181.66V176.67L167.95 175.8L163.41 151.75C160.91 137.24 155.64 129.03 136.43 126.53C159.59 123.02 167.66 111.73 167.66 99.7C167.66 82.26 153.58 72.14 127.93 72.14H75ZM113.41 77.71H121.62C136.43 77.71 144.2 85.63 144.2 100.14C144.2 113.49 137.9 124.48 121.48 124.48H112.97C112.97 107.62 112.97 92.52 113.41 77.71Z"/></defs><circle cx="128" cy="128" r="124" fill="url(#rdc-rimk)"/><circle cx="128" cy="128" r="108.2" fill="#02122a"/><circle cx="128" cy="128" r="106" fill="url(#rdc-facek)"/><circle cx="128" cy="34" r="3.7" fill="#8F9195"/><circle cx="140.27" cy="34.8" r="3.7" fill="#8F9195"/><circle cx="152.33" cy="37.2" r="3.7" fill="#8F9195"/><circle cx="163.97" cy="41.16" r="3.7" fill="#8F9195"/><circle cx="175" cy="46.59" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="175" cy="46.59" r="2.4" fill="#EF4135"/><circle cx="185.22" cy="53.42" r="3.7" fill="#8F9195"/><circle cx="194.47" cy="61.53" r="3.7" fill="#8F9195"/><circle cx="202.58" cy="70.78" r="3.7" fill="#8F9195"/><circle cx="209.41" cy="81" r="3.7" fill="#8F9195"/><circle cx="214.84" cy="92.03" r="3.7" fill="#8F9195"/><circle cx="218.8" cy="103.67" r="3.7" fill="#8F9195"/><circle cx="221.2" cy="115.73" r="3.7" fill="#8F9195"/><circle cx="222" cy="128" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="222" cy="128" r="2.4" fill="#EF4135"/><circle cx="221.2" cy="140.27" r="3.7" fill="#8F9195"/><circle cx="218.8" cy="152.33" r="3.7" fill="#8F9195"/><circle cx="214.84" cy="163.97" r="3.7" fill="#8F9195"/><circle cx="209.41" cy="175" r="3.7" fill="#8F9195"/><circle cx="202.58" cy="185.22" r="3.7" fill="#8F9195"/><circle cx="194.47" cy="194.47" r="3.7" fill="#8F9195"/><circle cx="185.22" cy="202.58" r="3.7" fill="#8F9195"/><circle cx="175" cy="209.41" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="175" cy="209.41" r="2.4" fill="#EF4135"/><circle cx="163.97" cy="214.84" r="3.7" fill="#8F9195"/><circle cx="152.33" cy="218.8" r="3.7" fill="#8F9195"/><circle cx="140.27" cy="221.2" r="3.7" fill="#8F9195"/><circle cx="128" cy="222" r="3.7" fill="#8F9195"/><circle cx="115.73" cy="221.2" r="3.7" fill="#8F9195"/><circle cx="103.67" cy="218.8" r="3.7" fill="#8F9195"/><circle cx="92.03" cy="214.84" r="3.7" fill="#8F9195"/><circle cx="81" cy="209.41" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="81" cy="209.41" r="2.4" fill="#EF4135"/><circle cx="70.78" cy="202.58" r="3.7" fill="#8F9195"/><circle cx="61.53" cy="194.47" r="3.7" fill="#8F9195"/><circle cx="53.42" cy="185.22" r="3.7" fill="#8F9195"/><circle cx="46.59" cy="175" r="3.7" fill="#8F9195"/><circle cx="41.16" cy="163.97" r="3.7" fill="#8F9195"/><circle cx="37.2" cy="152.33" r="3.7" fill="#8F9195"/><circle cx="34.8" cy="140.27" r="3.7" fill="#8F9195"/><circle cx="34" cy="128" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="34" cy="128" r="2.4" fill="#EF4135"/><circle cx="34.8" cy="115.73" r="3.7" fill="#8F9195"/><circle cx="37.2" cy="103.67" r="3.7" fill="#8F9195"/><circle cx="41.16" cy="92.03" r="3.7" fill="#8F9195"/><circle cx="46.59" cy="81" r="3.7" fill="#8F9195"/><circle cx="53.42" cy="70.78" r="3.7" fill="#8F9195"/><circle cx="61.53" cy="61.53" r="3.7" fill="#8F9195"/><circle cx="70.78" cy="53.42" r="3.7" fill="#8F9195"/><circle cx="81" cy="46.59" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="81" cy="46.59" r="2.4" fill="#EF4135"/><circle cx="92.03" cy="41.16" r="3.7" fill="#8F9195"/><circle cx="103.67" cy="37.2" r="3.7" fill="#8F9195"/><circle cx="115.73" cy="34.8" r="3.7" fill="#8F9195"/><use href="#rdc-R" transform="translate(0 4)" fill="#000" fill-opacity="0.6" filter="url(#rdc-shk)"/><use href="#rdc-R" transform="translate(2.2 3)" fill="#003a75"/><use href="#rdc-R" fill="url(#rdc-lgk)"/></svg>
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
 if(c&&typeof c==="object"){ if(c.age)td.append(ageSpan(c.age)); else{td.textContent=c.t;if(c.cls)td.className=c.cls;} }
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

/* ---- SHA-256 en JavaScript pur (la page d'Umbrel est en HTTP : l'outil de calcul du navigateur n'y est pas disponible) */
const K256=new Uint32Array([
 0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
 0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
 0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
 0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);
class Sha256{
 constructor(){
  this.h=new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  this.buf=new Uint8Array(64);this.n=0;this.len=0;this.w=new Uint32Array(64);
 }
 block(m,o){
  const w=this.w,h=this.h;
  for(let i=0;i<16;i++,o+=4)w[i]=(m[o]<<24)|(m[o+1]<<16)|(m[o+2]<<8)|m[o+3];
  for(let i=16;i<64;i++){
   const x=w[i-15],y=w[i-2];
   const s0=((x>>>7)|(x<<25))^((x>>>18)|(x<<14))^(x>>>3);
   const s1=((y>>>17)|(y<<15))^((y>>>19)|(y<<13))^(y>>>10);
   w[i]=(w[i-16]+s0+w[i-7]+s1)|0;
  }
  let a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],k=h[7];
  for(let i=0;i<64;i++){
   const S1=((e>>>6)|(e<<26))^((e>>>11)|(e<<21))^((e>>>25)|(e<<7));
   const t1=(k+S1+((e&f)^(~e&g))+K256[i]+w[i])|0;
   const S0=((a>>>2)|(a<<30))^((a>>>13)|(a<<19))^((a>>>22)|(a<<10));
   const t2=(S0+((a&b)^(a&c)^(b&c)))|0;
   k=g;g=f;f=e;e=(d+t1)|0;d=c;c=b;b=a;a=(t1+t2)|0;
  }
  h[0]=(h[0]+a)|0;h[1]=(h[1]+b)|0;h[2]=(h[2]+c)|0;h[3]=(h[3]+d)|0;
  h[4]=(h[4]+e)|0;h[5]=(h[5]+f)|0;h[6]=(h[6]+g)|0;h[7]=(h[7]+k)|0;
 }
 update(d){
  let i=0;const n=d.length;this.len+=n;
  if(this.n){
   const k=Math.min(64-this.n,n);this.buf.set(d.subarray(0,k),this.n);this.n+=k;i=k;
   if(this.n===64){this.block(this.buf,0);this.n=0;}
  }
  for(;i+64<=n;i+=64)this.block(d,i);
  if(i<n){this.buf.set(d.subarray(i),0);this.n=n-i;}
 }
 digest(){
  const bits=this.len*8,total=this.n<56?64:128,pad=new Uint8Array(total-this.n);
  pad[0]=0x80;
  const hi=Math.floor(bits/4294967296),lo=bits>>>0,L=pad.length;
  pad[L-8]=hi>>>24;pad[L-7]=hi>>>16;pad[L-6]=hi>>>8;pad[L-5]=hi;
  pad[L-4]=lo>>>24;pad[L-3]=lo>>>16;pad[L-2]=lo>>>8;pad[L-1]=lo;
  this.update(pad);
  let s="";for(let i=0;i<8;i++)s+=(this.h[i]>>>0).toString(16).padStart(8,"0");
  return s;
 }
}
/* empreinte d'un fichier lu par morceaux de 4 Mo (rien n'est envoye nulle part) ; renvoie null si la demande est annulee */
async function hashBlob(blob,onProg,cancelled){
 const h=new Sha256(),CH=4<<20;
 for(let o=0;o<blob.size;o+=CH){
  const buf=new Uint8Array(await blob.slice(o,o+CH).arrayBuffer());
  if(cancelled&&cancelled())return null;
  h.update(buf);
  onProg(Math.min(blob.size,o+CH)/blob.size);
  await new Promise(r=>setTimeout(r,0));
 }
 return h.digest();
}
const group=h=>h.replace(/(.{8})/g,"$1 ").trim();
function fmtSize(n){
 if(n<1024)return n+" octets";
 const u=["Ko","Mo","Go","To"];let i=-1;
 do{n/=1024;i++;}while(n>=1024&&i<u.length-1);
 return fmt(n,n<10?1:0)+" "+u[i];
}
function fmtDate(ts){
 return new Date(ts*1000).toLocaleString("fr-FR",{day:"numeric",month:"long",year:"numeric",hour:"2-digit",minute:"2-digit"});
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
/* un fichier lache a cote d'une zone de depot ne doit pas faire quitter la page */
window.addEventListener("dragover",e=>e.preventDefault());
window.addEventListener("drop",e=>e.preventDefault());

/* ---- zone de depot + calcul de l'empreinte */
function dropzone(onFile,label){
 const z=$("div","dz"),inp=$("input");
 inp.type="file";inp.hidden=true;
 z.append($("div","dzt",label),$("div","mu small","Le fichier reste sur ton appareil : seule son empreinte est utilisée."),inp);
 z.tabIndex=0;z.setAttribute("role","button");
 z.onclick=()=>inp.click();
 z.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();inp.click();}};
 ["dragenter","dragover"].forEach(ev=>z.addEventListener(ev,e=>{e.preventDefault();z.classList.add("over");}));
 ["dragleave","drop"].forEach(ev=>z.addEventListener(ev,e=>{e.preventDefault();z.classList.remove("over");}));
 z.addEventListener("drop",e=>{const f=e.dataTransfer&&e.dataTransfer.files;if(f&&f.length)onFile(f[0]);});
 inp.onchange=()=>{if(inp.files.length)onFile(inp.files[0]);inp.value="";};
 return z;
}
/* opts : label, text (ajoute la saisie d'un texte), onHash({hash,name,size,kind}), onReset() */
function picker(opts){
 const p={el:$("div","pk"),err:$("div","ferr")};
 let job=0;
 const pg=$("div","pg"),bar=$("i"),msg=$("div","mu small pgm");
 pg.append(bar);pg.hidden=msg.hidden=true;
 const dz=dropzone(f=>fromFile(f),opts.label);
 p.el.append(dz,pg,msg,p.err);
 let ta=null;
 if(opts.text){
  const det=$("details","prot tx");
  det.append($("summary","","Ou écris / colle un texte à la place"));
  ta=$("textarea");ta.rows=5;ta.placeholder="Le texte à horodater…";ta.spellcheck=false;
  const b=$("button","btn ghost","Calculer l'empreinte du texte");
  det.append($("p","","Chaque caractère compte, espaces et retours à la ligne compris : pour vérifier plus tard, il faudra le texte exactement identique."),ta,b);
  b.onclick=()=>fromText(ta.value);
  p.el.append(det);
 }
 function start(){job++;p.err.textContent="";if(opts.onReset)opts.onReset();return job;}
 async function run(blob,name,size,kind){
  const my=start();
  if(!size){p.err.textContent="Ce document est vide : il n'y a rien à horodater.";return;}
  pg.hidden=msg.hidden=false;bar.style.width="0%";msg.textContent="Calcul de l'empreinte… 0 %";
  try{
   const hx=await hashBlob(blob,f=>{if(my!==job)return;const q=Math.round(f*100);bar.style.width=q+"%";msg.textContent="Calcul de l'empreinte… "+q+" %";},()=>my!==job);
   if(my!==job||hx==null)return;
   pg.hidden=msg.hidden=true;
   opts.onHash({hash:hx,name:name,size:size,kind:kind});
  }catch(e){
   if(my!==job)return;
   pg.hidden=msg.hidden=true;p.err.textContent="Impossible de lire ce fichier. Réessaie, ou copie-le d'abord sur cet appareil.";
  }
 }
 function fromFile(f){return run(f,f.name,f.size,"file");}
 function fromText(t){
  const bytes=new TextEncoder().encode(t);
  return run(new Blob([bytes]),"Texte du "+new Date().toLocaleDateString("fr-FR"),bytes.length,"text");
 }
 p.reset=()=>{job++;pg.hidden=msg.hidden=true;p.err.textContent="";if(ta)ta.value="";};
 return p;
}

/* ---- horodater */
R.make=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Horodater un document"));
  const box=$("div","send");
  const show=w=>{rec.pickW.hidden=w!=="pick";rec.readyW.hidden=w!=="ready";rec.prevW.hidden=w!=="prev";rec.doneW.hidden=w!=="done";};
  rec.show=show;rec.busy=false;rec.token=null;rec.doc=null;
  /* 1. choisir */
  rec.pickW=$("div","");rec.wait=$("div","warn small");
  rec.pk=picker({label:"Dépose un fichier ici, ou touche pour le choisir",text:true,
   onHash:r=>{rec.doc=r;rec.nm.value=r.name;rec.hs.textContent=group(r.hash);
    rec.meta.textContent=(r.kind==="text"?"Texte de ":"Fichier de ")+fmtSize(r.size);rec.err.textContent="";show("ready");}});
  rec.pickW.append(rec.pk.el,rec.wait);
  /* 2. empreinte calculee */
  rec.readyW=$("div","");
  rec.meta=$("div","mu small");rec.hs=$("code","addr hx");
  rec.nm=$("input");rec.nm.type="text";rec.nm.maxLength=120;rec.nm.autocomplete="off";
  rec.go=$("button","btn","Vérifier avant d'horodater");rec.other=$("button","btn ghost","Choisir un autre document");rec.err=$("div","ferr");
  const r1=$("div","btnrow");r1.append(rec.go,rec.other);
  rec.readyW.append($("label","","Empreinte SHA-256 du document"),rec.meta,rec.hs,
   $("label","","Nom pour t'y retrouver (reste chez toi, jamais dans la chaîne)"),rec.nm,r1,rec.err);
  /* 3. apercu */
  rec.prevW=$("div","");
  rec.prows=$("div","prows");rec.pw=$("input");rec.pw.type="password";rec.pw.autocomplete="off";rec.pw.placeholder="Mot de passe du portefeuille";
  rec.pwl=$("label","","Mot de passe du portefeuille");
  rec.ok=$("button","btn","Confirmer l'horodatage");rec.back=$("button","btn ghost","Modifier");rec.perr=$("div","ferr");
  const r2=$("div","btnrow");r2.append(rec.ok,rec.back);
  rec.prevW.append($("div","ptitle","Vérifie avant de confirmer"),rec.prows,
   $("div","warn small","Une fois écrit dans la chaîne, un horodatage ne peut plus être retiré."),rec.pwl,rec.pw,r2,rec.perr);
  /* 4. fini */
  rec.doneW=$("div","");
  rec.dmsg=$("div","dmsg");rec.dtx=$("code","addr");rec.dwarn=$("div","warn small");
  rec.dnote=$("div","mu small","Ton document sera daté dès qu'un bloc contenant cette transaction aura été trouvé. En minage solo, cela peut prendre du temps. Quand l'horodatage affiche « 1 confirmation » dans « Mes horodatages », télécharge son certificat et garde-le avec ton document.");
  rec.dnew=$("button","btn ghost","Horodater un autre document");
  rec.doneW.append(rec.dmsg,rec.dwarn,$("div","mu small","Identifiant de la transaction"),rec.dtx,rec.dnote,rec.dnew);
  box.append(rec.pickW,rec.readyW,rec.prevW,rec.doneW);rec.el.append(box);
  const lock=b=>{rec.busy=b;rec.go.disabled=b||!rec.can;rec.ok.disabled=b;rec.back.disabled=b;rec.other.disabled=b;};
  const reset=()=>{rec.token=null;rec.doc=null;rec.pw.value="";rec.nm.value="";rec.err.textContent="";rec.perr.textContent="";rec.pk.reset();show("pick");};
  rec.other.onclick=reset;rec.dnew.onclick=reset;
  rec.go.onclick=async()=>{
   if(!rec.doc)return;
   rec.err.textContent="";lock(true);rec.go.textContent="Vérification…";
   const j=await post("api/preview",{hash:rec.doc.hash,name:rec.nm.value,size:rec.doc.size});
   rec.go.textContent="Vérifier avant d'horodater";lock(false);
   if(!j.ok){rec.err.textContent=j.error||"Erreur.";return;}
   rec.token=j.token;rec.prows.textContent="";
   const row=(a,b,cls)=>{const r=$("div","prow");r.append($("span","mu",a),$("b",cls||"",b));rec.prows.append(r);};
   row("Document",j.name,"nm2");
   const hh=$("div","prow col");hh.append($("span","mu","Empreinte qui sera écrite dans la chaîne"),$("code","addr hx",group(j.hash)));rec.prows.append(hh);
   row("Ce qui est écrit dans la chaîne","Seulement l'empreinte (36 octets)");
   row("Frais du réseau (environ)",j.fee+" RDT","big2");
   rec.pw.value="";rec.perr.textContent="";
   rec.pwl.hidden=rec.pw.hidden=!rec.encrypted;
   show("prev");if(rec.encrypted)rec.pw.focus();
  };
  rec.back.onclick=()=>{rec.token=null;rec.pw.value="";show("ready");};
  const confirm=async()=>{
   if(rec.busy||!rec.token)return;
   rec.perr.textContent="";lock(true);rec.ok.textContent="Envoi…";
   const j=await post("api/stamp",{token:rec.token,passphrase:rec.pw.value});
   rec.ok.textContent="Confirmer l'horodatage";lock(false);
   if(!j.ok){rec.perr.textContent=j.error||"Erreur.";
    if(/expiré|déjà servi|déjà été|vient d'être/.test(j.error||"")){rec.token=null;rec.err.textContent=j.error;rec.pw.value="";show("ready");}
    return;}
   rec.token=null;rec.pw.value="";
   rec.dmsg.textContent="Horodatage envoyé ✓";rec.dwarn.textContent=j.warn||"";rec.dtx.textContent=j.txid;
   show("done");poll();
  };
  rec.ok.onclick=confirm;rec.pw.addEventListener("keydown",e=>{if(e.key==="Enter")confirm();});
  show("pick");
 }
 rec.encrypted=d.encrypted;rec.can=d.can_stamp;
 rec.wait.textContent=d.can_stamp?"":"Horodatage bloqué : le nœud n'est pas encore à jour.";
 if(!rec.busy)rec.go.disabled=!d.can_stamp;
 if(rec.prevW.hidden===false){rec.pwl.hidden=rec.pw.hidden=!d.encrypted;}
};

/* ---- verifier */
R.check=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Vérifier un document"));
  const box=$("div","send");
  rec.cert=null;rec.last=null;
  rec.out=$("div","res");rec.out.hidden=true;
  rec.pk=picker({label:"Dépose ici le document à vérifier",text:true,
   onReset:()=>{rec.out.hidden=true;rec.last=null;},
   onHash:r=>{rec.last=r;run();}});
  /* certificat (facultatif) */
  const det=$("details","prot cert");
  det.append($("summary","","J'ai un certificat (fichier .json)"));
  rec.cin=$("input");rec.cin.type="file";rec.cin.accept=".json,application/json";
  rec.cmsg=$("div","mu small");
  det.append($("p","","Si l'horodatage vient d'un autre nœud Radthal, ou si tu l'as fait avant, choisis son certificat : ton nœud vérifiera la transaction indiquée."),rec.cin,rec.cmsg);
  rec.cin.onchange=async()=>{
   rec.cert=null;rec.cmsg.textContent="";
   const f=rec.cin.files[0];if(!f)return;
   try{
    if(f.size>200000)throw 0;
    const j=JSON.parse(await f.text());
    if(!j||j.format!=="radthal-timestamp"||!/^[0-9a-f]{64}$/.test(j.txid||"")||!/^[0-9a-f]{64}$/.test(j.hash||""))throw 0;
    rec.cert=j;rec.cmsg.textContent="Certificat chargé : "+(j.file_name||"document")+".";
   }catch(e){rec.cmsg.textContent="Ce fichier n'est pas un certificat d'horodatage Radthal.";}
   if(rec.last)run();
  };
  box.append(rec.pk.el,det,rec.out);rec.el.append(box);
  async function run(){
   const r=rec.last;if(!r)return;
   rec.out.hidden=false;rec.out.className="res";rec.out.textContent="Vérification…";
   if(rec.cert&&rec.cert.hash!==r.hash){
    return show("bad","Ce certificat ne correspond pas à ce document","L'empreinte du document n'est pas celle du certificat : ce n'est pas le même contenu, ou le document a été modifié (même d'une virgule).",r);
   }
   const body={hash:r.hash};
   if(rec.cert){body.txid=rec.cert.txid;if(rec.cert.block&&rec.cert.block.hash)body.blockhash=rec.cert.block.hash;if(rec.cert.genesis)body.genesis=rec.cert.genesis;}
   const j=await post("api/verify",body);
   if(rec.last!==r)return;
   if(!j.ok)return show("bad","Vérification impossible",j.error||"Erreur.",r);
   const st=j.status;
   if(st==="ok"){
    const l=["Ce document existait au plus tard le "+fmtDate(j.blocktime)+".","Bloc "+j.height+" · "+j.conf+" confirmation"+(j.conf>1?"s":"")];
    show("good","✓ Horodatage vérifié",l,r,j.txid);
   }else if(st==="pending")show("warn","⏳ Horodaté, en attente d'un bloc","La transaction est connue de ton nœud mais aucun bloc ne la contient encore. Le document sera daté dès qu'un bloc sera trouvé.",r,j.txid);
   else if(st==="none")show("bad","Aucun horodatage trouvé pour ce document","Ce nœud n'a pas d'horodatage pour ce contenu exact. Si le document a été modifié, même d'une virgule, son empreinte change. Si l'horodatage vient d'ailleurs, ouvre « J'ai un certificat ».",r);
   else if(st==="mismatch")show("bad","Le certificat ne correspond pas à ce document","La transaction du certificat n'enregistre pas l'empreinte de ce document : le document a été modifié, ou ce n'est pas le bon certificat.",r);
   else if(st==="notfound")show("warn","Transaction introuvable","Ton nœud ne connaît pas cette transaction. Il n'est peut-être pas encore à jour, ou le certificat vient d'une autre chaîne.",r);
   else if(st==="other-chain")show("bad","Autre chaîne","Ce certificat vient d'une autre chaîne que celle de ton nœud.",r);
   else if(st==="stale")show("warn","Bloc abandonné","Le bloc du certificat ne fait plus partie de la chaîne principale. L'horodatage n'est plus valable tel quel.",r);
   else show("bad","Résultat inconnu","Réponse inattendue du serveur.",r);
  }
  function show(tone,title,lines,r,txid){
   rec.out.className="res r"+tone[0];rec.out.textContent="";
   rec.out.append($("div","rt",title));
   (Array.isArray(lines)?lines:[lines]).forEach(t=>rec.out.append($("div","rl",t)));
   rec.out.append($("div","mu small rl","Empreinte du document : "+group(r.hash)));
   if(txid)rec.out.append($("div","mu small rl","Transaction : "+txid));
  }
 }
};

/* ---- la liste */
R.stamps=(rec,d)=>{
 if(!rec.el){
  rec.el=$("section","sec");rec.el.append($("h2","","Mes horodatages"));
  rec.box=$("div","stl");rec.foot=$("div","mu small stfoot");rec.map=new Map();
  rec.empty=$("div","empty","Aucun horodatage pour l'instant.");
  rec.dl=$("a","cp","Télécharger la liste (sauvegarde)");rec.dl.href="registre.json";rec.dl.download="horodatages-radthal.json";
  rec.fl=$("span","");rec.foot.append(rec.dl," ",rec.fl);
  rec.el.append(rec.box,rec.foot);
 }
 const seen=new Set(),els=[];
 d.rows.forEach(x=>{
  let r=rec.map.get(x.txid);
  if(!r){
   r={el:$("div","st"),dt:$("div","dt"),nm:$("div","nm"),hh:$("div","hh"),bd:$("span","badge"),act:$("div","act"),inf:$("div","mu small")};
   const left=$("div","");left.append(r.dt,r.bd);
   const mid=$("div","");mid.append(r.nm,r.hh,r.inf);
   r.cert=$("a","cp","Certificat");r.cert.download="";
   r.cp=$("button","cp","Copier l'empreinte");
   r.cp.onclick=()=>copyTxt(r.hash,r.cp,"Copier l'empreinte");
   r.act.append(r.cert,r.cp);
   r.el.append(left,mid,r.act);rec.map.set(x.txid,r);
  }
  r.hash=x.hash;
  r.nm.textContent=x.name+(x.size!=null?" · "+fmtSize(x.size):"");
  r.hh.textContent=x.hash.slice(0,12)+"…"+x.hash.slice(-8);r.hh.title=x.hash;
  let tone="",label="",inf="";
  if(x.state==="ok"){r.dt.textContent=x.blocktime?fmtDate(x.blocktime):"Confirmé";tone="good";label=x.conf+" confirmation"+(x.conf>1?"s":"");}
  else if(x.state==="pending"){r.dt.textContent="En attente d'un bloc";tone="warn";label="en attente";
   inf=x.inpool?"":"Cette transaction n'est plus dans la file d'attente du nœud : si elle ne revient pas, refais l'horodatage.";}
  else if(x.state==="lost"){r.dt.textContent="Transaction introuvable";tone="bad";label="perdue";inf="Le portefeuille ne connaît plus cette transaction (expirée ?). Tu peux refaire l'horodatage de ce document.";}
  else if(x.state==="rejected"){r.dt.textContent="Transaction annulée";tone="bad";label="annulée";inf="Cette transaction est en conflit avec une autre. Tu peux refaire l'horodatage.";}
  else{r.dt.textContent="État inconnu";label="inconnu";inf="Le nœud ne répond pas pour l'instant.";}
  r.bd.className="badge "+tone;r.bd.textContent=label;
  r.inf.textContent=inf;r.inf.hidden=!inf;
  if(x.state==="ok"){r.cert.href="cert/"+x.txid+".json";r.cert.hidden=false;}else{r.cert.removeAttribute("href");r.cert.hidden=true;}
  seen.add(x.txid);els.push(r.el);
 });
 for(const [k,r] of rec.map)if(!seen.has(k)){r.el.remove();rec.map.delete(k);}
 rec.empty.remove();
 if(!els.length)rec.box.append(rec.empty);else order(rec.box,els);
 rec.fl.textContent=d.total>d.rows.length?d.total+" horodatages au total, les "+d.rows.length+" derniers sont affichés. ":d.total+" horodatage"+(d.total>1?"s":"")+". ";
 rec.fl.textContent+="La liste est gardée dans "+d.dir+" sur le Raspberry Pi.";
};

/* ---- comment ca marche */
R.about=(rec,d)=>{
 if(rec.el)return;
 rec.el=$("details","prot");
 rec.el.append($("summary","","Comment ça marche ?"));
 [["Une empreinte, pas le document.","La page calcule l'empreinte SHA-256 de ton document sur ton appareil : une suite de 64 caractères qui change complètement si une seule lettre du document change. Le document ne quitte jamais ton appareil."],
  ["Gravée dans la chaîne.","Cette empreinte est écrite dans une transaction Radthal. Quand un bloc contient cette transaction, l'empreinte porte la date de ce bloc : elle ne peut plus être modifiée."],
  ["Ce que ça prouve.","Le document existait, tel quel, au plus tard à la date du bloc. Ça ne prouve pas qui l'a écrit. L'heure d'un bloc est fixée par son mineur et peut s'écarter de l'heure réelle de quelques heures."],
  ["Garde ton document et ton certificat.","Sans le document exact, l'empreinte ne sert à rien. Le certificat (fichier .json) permet à quelqu'un d'autre de retrouver la transaction sur un nœud Radthal."],
  ["Attention à la confidentialité.","N'horodate pas un texte court et facile à deviner : quelqu'un pourrait tester des textes jusqu'à retrouver l'empreinte."]
 ].forEach(([t,x])=>{const p=$("p","");p.append($("b","tx",t)," "+x);rec.el.append(p);});
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

const UI_VER="__UIVER__";
function uiBusy(){
 const a=document.activeElement;
 if(a&&(a.tagName==="INPUT"||a.tagName==="TEXTAREA"||a.tagName==="SELECT"))return true;
 return [...document.querySelectorAll("input,textarea")].some(e=>e.offsetParent!==null&&(e.type==="file"?(e.files&&e.files.length>0):(e.type==="checkbox"||e.type==="radio"||e.type==="button"||e.type==="submit")?false:e.value!==e.defaultValue));
}
function uiNew(){
 /* nouvelle version installee : on recharge la page, sauf si tu es en train de saisir quelque chose */
 if(uiBusy()){
  if(!document.getElementById("uibar")){
   const b=document.createElement("div");b.id="uibar";
   b.style.cssText="position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:99999;background:#131a2c;border:2px solid #ff7a2f;color:#f2f4f8;border-radius:14px;padding:12px 18px;font:600 15px system-ui,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.5);cursor:pointer;max-width:92vw;text-align:center";
   b.textContent="Nouvelle version installée : touche ici pour recharger (rechargement automatique dès que tu as fini de saisir)";
   b.onclick=()=>location.reload();document.body.appendChild(b);
  }
  return false;
 }
 try{const t=+sessionStorage.getItem("uiRl")||0;if(Date.now()-t<15000)return false;sessionStorage.setItem("uiRl",String(Date.now()));}catch(e){}
 location.reload();return true;
}
function setPill(ok,txt){const p=document.getElementById("pill");p.className="pill "+(ok?"ok":"ko");document.getElementById("pilltx").textContent=txt;}
async function poll(){
 try{
  const r=await fetch("api/state",{cache:"no-store"});if(!r.ok)throw new Error(r.status);
  const s=await r.json();if(s.ui&&s.ui!==UI_VER&&uiNew())return;offset=s.now-Date.now()/1000;lastOk=Date.now();
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
    return (HTML.replace("__TITLE__", TITLES.get(VIEW, "Radthal")).replace("__VIEW__", VIEW).replace("__UIVER__", UI_VER)).encode("utf-8")


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            self._send(200, json.dumps(dict(get_state(), ui=UI_VER), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send(200, page(), "text/html; charset=utf-8")
        elif VIEW == "stamp" and path == "/registre.json":
            self._send(200, reg_export(), "application/json; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="horodatages-radthal.json"'})
        elif VIEW == "stamp" and re.fullmatch(r"/cert/[0-9a-f]{64}\.json", path):
            try:
                c = certificate(path[6:70])
                body = json.dumps(c, ensure_ascii=False, indent=1).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="horodatage-radthal-%s.json"' % c["hash"][:12]})
            except UserError as e:
                self._send(409, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            except Exception:
                self._send(500, b"erreur interne", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if VIEW != "stamp" or path not in ("/api/preview", "/api/stamp", "/api/verify"):
            return self._send(404, b"not found", "text/plain")
        # une page d'un autre site ne doit pas pouvoir declencher un horodatage : JSON obligatoire (ce qui force
        # une verification du navigateur que ce serveur ne l'autorise jamais) et refus des requetes inter-sites
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return self._json(403, {"ok": False, "error": "Requête refusée."})
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"ok": False, "error": "Requête refusée."})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 4096:
                raise ValueError
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self._json(400, {"ok": False, "error": "Requête illisible."})
        try:
            if path == "/api/preview":
                out = stamp_preview(data.get("hash"), data.get("name"), data.get("size"))
            elif path == "/api/stamp":
                out = stamp_send(data.get("token"), data.get("passphrase", ""))
            else:
                out = verify(data.get("hash"), data.get("txid"), data.get("blockhash"), data.get("genesis"))
            self._json(200, dict(out, ok=True))
        except UserError as e:
            self._json(200, {"ok": False, "error": str(e)})
        except Exception:
            self._json(500, {"ok": False, "error": "Erreur interne. Réessaie."})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if VIEW != "stamp":
        threading.Thread(target=wallet_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
