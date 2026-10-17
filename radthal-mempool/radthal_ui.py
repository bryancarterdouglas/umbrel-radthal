"""Interface web des applications Umbrel Radthal (noeud, tableau de bord, mempool, ckpool).

Un seul fichier, aucune dependance (Python 3 seulement). Le serveur lit le noeud (RPC) et les
fichiers de ckpool, et sert :
  /           la page (HTML + CSS + JS, style anime comme la video de presentation)
  /api/state  les donnees de la vue choisie par la variable VIEW, en JSON

La page interroge /api/state toutes les 3 secondes et met l'affichage a jour sans recharger.
"""
import base64, glob, json, math, os, re, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIEW = os.environ.get("VIEW", "dashboard")
RPC = os.environ.get("RPC", "http://radthal-host:9332")
RPCUSER = os.environ.get("RPCUSER", "radthal")
PASSFILE = os.environ.get("PASSFILE", "/run/secrets/radthal_rpc_password")
LOGS = os.environ.get("LOGS", "/ckpool/logs")
PORT = int(os.environ.get("PORT", "8000"))
WALLET = os.environ.get("WALLET", "minage")
STRATUM = os.environ.get("STRATUM_PORT", "3333")
TITLES = {"node": "Nœud Radthal", "dashboard": "Tableau de bord Radthal",
          "mempool": "Mempool Radthal", "ckpool": "ckpool Radthal"}
SUBTITLES = {"node": "L'état de ton nœud complet",
             "dashboard": "La chaîne Radthal en direct",
             "mempool": "Les transactions en attente d'un bloc",
             "ckpool": "Ton mineur et ton pool solo"}


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


VIEWS = {"node": view_node, "dashboard": view_dashboard, "mempool": view_mempool, "ckpool": view_ckpool}

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
        if _cache["data"] is None or time.time() - _cache["t"] > 2.0:
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

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=wallet_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
