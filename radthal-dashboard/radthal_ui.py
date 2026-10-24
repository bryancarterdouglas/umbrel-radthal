"""Interface web des applications Umbrel Radthal (noeud, tableau de bord, mempool, ckpool).

Un seul fichier, aucune dependance (Python 3 seulement). Le serveur lit le noeud (RPC) et les
fichiers de ckpool, et sert :
  /           la page (HTML + CSS + JS, style anime comme la video de presentation)
  /api/state  les donnees de la vue choisie par la variable VIEW, en JSON
  /api/search, /api/block, /api/mine   l'explorateur de blocs (lecture seule)

La page interroge /api/state toutes les 3 secondes et met l'affichage a jour sans recharger.
"""
import base64, glob, json, math, os, re, threading, time, urllib.parse, urllib.request
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



# ---------------------------------------------------------------- surveillance des forks

FORK_WINDOW = 144     # on ne regarde que les branches des 144 derniers blocs (environ une journee)
REF_DEPTH = 1000      # le bloc de reference conseille est environ 1 000 blocs sous la tete
AHEAD_DELAY = 30      # une annonce de chaine plus longue ne compte qu'apres 30 s (sinon on voit un faux clignotement a chaque nouveau bloc)
FORK_EVERY = int(os.environ.get("FORK_EVERY", "1800"))   # getchaintips renvoie TOUTES les branches que le noeud a connues (sur ta chaine :
FORK_MIN = int(os.environ.get("FORK_MIN", "300"))       # plus de 470 000) : on l'appelle rarement, en arriere-plan, et on lit la reponse en flux.
_FK = {"sec": None, "busy": False, "last": 0.0, "key": None, "ok_t": 0.0, "fail": 0, "dur": None, "n": None, "ahead_since": None}
_FK_LOCK = threading.Lock()     # protege les valeurs conseillees (advice_cached)
_FKT_LOCK = threading.Lock()    # protege l'etat de l'analyse des forks (jamais tenu pendant un appel au noeud)


def _when(bh):
    """Il y a combien de temps ce bloc (meme d'une branche abandonnee) a ete trouve."""
    hd = rpc("getblockheader", [bh]) if bh else None
    return (time.time() - hd["time"]) if hd and hd.get("time") else None


def _ago(s):
    if s is None or s < 0:
        return "à l'instant"   # l'horloge d'un bloc peut avoir quelques secondes d'avance
    return "il y a " + dur(s)


_TIP_RE = re.compile(rb'\{"height":(\d+),"hash":"([0-9a-f]{64})","branchlen":(\d+),"status":"([a-z-]+)"\}')


def chaintips_recent(min_height, timeout=180):
    """getchaintips renvoie TOUTES les branches connues du noeud : sur la chaine Radthal, plus de 470 000 (surtout d'anciens
    blocs orphelins), soit une reponse de ~90 Mo. On la lit par morceaux d'1 Mo et on ne garde que les branches recentes
    (et la branche principale) : la liste entiere n'est jamais construite en memoire. None si le noeud ne repond pas."""
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        body = json.dumps({"jsonrpc": "1.0", "id": "u", "method": "getchaintips", "params": []}).encode()
        req = urllib.request.Request(RPC, body, {"Content-Type": "application/json", "Authorization": auth})
        out, total, buf, raw = [], 0, b"", b""
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                if raw is not None:                       # petite reponse : on la garde pour un secours si le format change
                    raw += chunk
                    if len(raw) > (4 << 20):
                        raw = None
                buf += chunk
                last = 0
                for m in _TIP_RE.finditer(buf):
                    last = m.end()
                    total += 1
                    ht, st = int(m.group(1)), m.group(4).decode()
                    if st == "active" or ht >= min_height:
                        out.append({"height": ht, "hash": m.group(2).decode(), "branchlen": int(m.group(3)), "status": st})
                buf = buf[last:]
        if total == 0 and raw:
            res = json.loads(raw).get("result")
            return res if isinstance(res, list) else None
        _FK["n"] = total
        return out
    except Exception:
        return None


def fork_tips(h):
    return chaintips_recent(max(0, h - FORK_WINDOW))


def _fork_finish(state, lines):
    if state == "ok":
        label = "Aucun fork détecté"
        detail = "Les %d derniers blocs forment une seule chaîne." % FORK_WINDOW if not lines else "Aucun fork en cours."
    elif state == "warn":
        label = "Fork possible : patiente"
        detail = "Attends quelques blocs avant d'accepter un paiement important."
    else:
        label = "Fork détecté : prudence"
        detail = "N'accepte aucun paiement important avant d'avoir compris ce qui se passe, et préviens les autres propriétaires de nœuds Radthal."
    return {"id": "fork", "type": "fork", "state": state, "label": label, "detail": detail, "lines": lines}


def fork_section(info):
    """Voyant « fork detecte » : cherche dans le noeud (getchaintips, lu en flux) des branches concurrentes de ta chaine.
    - vert   : une seule chaine (un simple bloc orphelin du passe reste vert, avec une phrase d'explication) ;
    - orange : branche concurrente a la hauteur de ta chaine, ou branche de 2 a 5 blocs (chaine plus longue annoncee : fork_ahead) ;
    - rouge  : branche invalide rejetee, ou branche concurrente de 6 blocs ou plus."""
    h = info.get("blocks")
    tips = fork_tips(h) if h is not None else None
    if h is None or not isinstance(tips, list):
        return None
    live, past, invalid = [], [], []
    for t in tips:
        st, th = t.get("status"), t.get("height")
        if st == "active" or th is None or th < h - FORK_WINDOW:
            continue
        if st == "invalid":
            invalid.append(t)
        elif th > h:
            if st == "valid-fork":                 # bloc complet plus haut que le tien, sur une autre branche
                live.append(t)                     # (les simples en-tetes plus hauts : voir fork_ahead)
        elif th == h:
            live.append(t)
        else:
            past.append(t)
    lines, state = [], "ok"
    rank = {"ok": 0, "warn": 1, "bad": 2}

    def raise_to(s):
        nonlocal state
        if rank[s] > rank[state]:
            state = s
    for t in invalid:
        raise_to("bad")
        lines.append("Ton nœud a rejeté une branche invalide (jusqu'au bloc %s, %s) : "
                     "quelqu'un a envoyé des blocs qui ne respectent pas les règles." % (fr_int(t["height"]), _ago(_when(t.get("hash")))))
    for t in live:
        raise_to("bad" if t["branchlen"] >= 6 else "warn")
        lines.append("Une branche concurrente de %s est arrivée à la hauteur de ta chaîne (bloc %s). "
                     "Elle se règle normalement au prochain bloc." % (plural(t["branchlen"], "bloc"), fr_int(t["height"])))
    one, deep = [], []
    for t in sorted(past, key=lambda x: -x["height"]):
        (deep if t["branchlen"] >= 2 else one).append(t)
    for t in deep[:3]:
        raise_to("bad" if t["branchlen"] >= 6 else "warn")
        lines.append("Une branche concurrente de %s n'a pas été retenue (jusqu'au bloc %s, %s)."
                     % (plural(t["branchlen"], "bloc"), fr_int(t["height"]), _ago(_when(t.get("hash")))))
    for t in deep[3:]:
        raise_to("bad" if t["branchlen"] >= 6 else "warn")
    if len(deep) > 3:
        lines.append("Et %d autres branches concurrentes dans les %d derniers blocs." % (len(deep) - 3, FORK_WINDOW))
    if one:
        t = one[0]
        lines.append("%s orphelin%s dans les %d derniers blocs (le dernier au bloc %s, %s). Sans gravité : "
                     "deux blocs ont été trouvés en même temps, un seul est resté."
                     % (plural(len(one), "bloc"), "s" if len(one) > 1 else "", FORK_WINDOW, fr_int(t["height"]), _ago(_when(t.get("hash")))))
    return _fork_finish(state, lines)


def fork_ahead(info):
    """Chaine plus longue annoncee par des pairs : lu sur getblockchaininfo (instantane). Le nombre d'en-tetes depasse celui
    des blocs. Ne compte qu'apres AHEAD_DELAY secondes (sinon faux clignotement a chaque nouveau bloc) et jamais pendant
    le telechargement initial. Renvoie la phrase a afficher, ou None."""
    h, hd = info.get("blocks"), info.get("headers")
    with _FKT_LOCK:
        if h is None or hd is None or hd <= h or info.get("initialblockdownload"):
            _FK["ahead_since"] = None
            return None
        if _FK["ahead_since"] is None:
            _FK["ahead_since"] = time.time()
        if time.time() - _FK["ahead_since"] < AHEAD_DELAY:
            return None
    return ("Des pairs annoncent une chaîne plus longue que la tienne (bloc %s contre %s) : "
            "ton nœud est peut-être en retard ou sur une autre branche." % (fr_int(hd), fr_int(h)))


def _fork_worker(info):
    t0 = time.time()
    try:
        sec = fork_section(info)
    except Exception:
        sec = None
    with _FKT_LOCK:
        _FK["busy"], _FK["dur"] = False, time.time() - t0
        if sec is not None:
            _FK["sec"], _FK["ok_t"], _FK["fail"] = sec, time.time(), 0
        else:
            _FK["fail"] += 1


def fork_wait(failed):
    """Section neutre (voyant gris) tant qu'aucune analyse n'a abouti."""
    if failed:
        label, detail = "Analyse des forks indisponible", "Le nœud n'a pas répondu à temps. Nouvel essai dans un instant."
    else:
        label, detail = "Surveillance des forks", "Première analyse en cours : sur une longue chaîne, elle prend quelques secondes."
    return {"id": "fork", "type": "fork", "state": "wait", "label": label, "detail": detail, "lines": []}


def fork_cached(info):
    """Ne fait JAMAIS attendre la page : l'analyse des branches (lourde) tourne en arriere-plan, une seule a la fois, toutes
    les FORK_EVERY secondes, ou apres un nouveau bloc (au plus toutes les FORK_MIN secondes), plus rarement encore si elle
    est lente. La chaine plus longue annoncee est lue, elle, a chaque affichage (fork_ahead). Sans analyse recente, la page
    affiche un voyant gris plutot qu'un faux « tout va bien »."""
    key = (info.get("blocks"), info.get("bestblockhash"))
    now = time.time()
    dur = _FK["dur"] or 0
    every, lo = max(FORK_EVERY, 30 * dur), max(FORK_MIN, 10 * dur)
    with _FKT_LOCK:
        if not _FK["busy"]:
            gap = now - _FK["last"]
            if _FK["last"] == 0.0 or gap >= every or (key != _FK["key"] and gap >= lo) or (_FK["fail"] and gap >= lo):
                _FK["busy"], _FK["last"], _FK["key"] = True, now, key
                threading.Thread(target=_fork_worker, args=(dict(info),), daemon=True).start()
        sec, ok_t, fail = _FK["sec"], _FK["ok_t"], _FK["fail"]
    ah = fork_ahead(info)
    if sec is None or now - ok_t > max(600, 3 * every):
        return _fork_finish("warn", [ah]) if ah else fork_wait(fail > 0)
    if ah:
        return _fork_finish("bad" if sec["state"] == "bad" else "warn", [ah] + sec["lines"])
    return sec


_AV = {"t": 0.0, "ref": None, "sec": None}


def advice_cached(info):
    """Les valeurs ne changent que tous les 100 blocs : on ne recalcule que si le bloc de reference change (ou toutes les 5 min)."""
    h = info.get("blocks") or 0
    ref = (h - REF_DEPTH) // 100 * 100
    with _FK_LOCK:
        if _AV["sec"] is None or _AV["ref"] != ref or time.time() - _AV["t"] > 300:
            _AV["sec"], _AV["t"], _AV["ref"] = advice_section(info), time.time(), ref
        return _AV["sec"]


def advice_section(info):
    """Valeurs conseillees pour la PROCHAINE version du noeud (a recopier dans chainparams.cpp) : un bloc de reference
    environ 1 000 blocs sous la tete (arrondi aux 100 blocs). Bitcoin Core n'a plus de checkpoints : ces deux valeurs ne
    protegent que les nouveaux noeuds pendant leur synchronisation, pas une chaine deja etablie."""
    h = info.get("blocks")
    if not h or h < REF_DEPTH + 100:
        return None
    ref = (h - REF_DEPTH) // 100 * 100
    bh = rpc("getblockhash", [ref])
    hd = rpc("getblockheader", [bh]) if bh else None
    if not hd or not hd.get("chainwork"):
        return None
    return {"id": "advice", "type": "advice", "title": "Pour la prochaine version du nœud (conseillé)", "items": [
        {"label": "Bloc de référence", "value": "%s (%s blocs sous la tête)" % (fr_int(ref), fr_int(h - ref)), "copy": False},
        {"label": "defaultAssumeValid", "value": bh, "copy": True},
        {"label": "nMinimumChainWork", "value": hd["chainwork"], "copy": True}],
        "warn": "À recopier dans chainparams.cpp seulement lors d'une nouvelle version du nœud. Ces deux valeurs aident les nouveaux nœuds "
                "à ne pas se laisser tromper pendant leur synchronisation ; elles n'empêchent pas la réécriture d'une chaîne déjà établie "
                "(Bitcoin Core n'a plus de points de contrôle)."}


# ---------------------------------------------------------------- explorateur de blocs (version simple de mempool.space)

XP_PAGE = 20          # transactions par page
XP_CAP = 12           # entrees et sorties detaillees par transaction (le reste est compte)
XP_SEM = threading.BoundedSemaphore(4)      # au plus 4 explorations en meme temps : le noeud reste tranquille


class XpError(Exception):
    pass


def _clean(s, n=300):
    """Texte venant de la chaine (donc ecrit par n'importe qui) : on retire les caracteres de controle et
    invisibles, on coupe. La page l'affiche en texte brut, jamais comme du HTML."""
    s = "".join(ch if (ch.isprintable() or ch in " \n") else "·" for ch in str(s))
    s = re.sub(r"\n{2,}", "\n", s).strip()
    return s[:n]


def coinbase_tag(hx):
    """Le texte lisible de la coinbase (souvent le nom du pool) ; vide si ce n'est que du binaire.
    On ne garde que les passages d'au moins 4 caracteres imprimables contenant des lettres : les octets binaires
    (hauteur du bloc, nombres aleatoires) ne produisent plus de petits morceaux illisibles."""
    try:
        raw = bytes.fromhex(hx or "")
    except Exception:
        return ""
    runs = re.findall(rb"[\x20-\x7e]{4,}", raw)
    keep = [r.decode("ascii").strip() for r in runs if len(re.findall(rb"[A-Za-z]", r)) >= 3]
    return " ".join(k for k in keep if k)[:120]


def _readable(data):
    """Le texte si ces octets en sont vraiment (UTF-8 valide, caracteres imprimables), sinon None.
    Avant, decode(..., "replace") remplacait chaque octet binaire par un caractere imprimable : de l'illisible etait affiche."""
    try:
        txt = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not txt or "\ufffd" in txt:
        return None
    ok = sum(1 for ch in txt if ch.isprintable() or ch in " \n")
    if ok < len(txt) or not any(ch.isalnum() for ch in txt):
        return None
    return txt


def opreturn_text(spk):
    parts = [p for p in (spk.get("asm") or "").split(" ")[1:] if re.fullmatch(r"(?:[0-9a-f]{2})+", p)]
    data = b"".join(bytes.fromhex(p) for p in parts)
    if not data:
        return {"label": "Donnée vide", "text": ""}
    if data[:4] == b"RDTS" and len(data) == 36:
        return {"label": "Horodatage Radthal", "text": "empreinte SHA-256 : " + data[4:].hex()}
    if data[:4] == bytes.fromhex("aa21a9ed"):
        return {"label": "Donnée technique du bloc (SegWit)", "text": "hex : " + data.hex()[:160]}
    txt = _readable(data)
    if txt is None:
        return {"label": "Donnée binaire (OP_RETURN)", "text": "hex : " + data.hex()[:160]}
    if txt.startswith("RDTM"):
        return {"label": "Message Radthal", "text": _clean(txt[4:])}
    return {"label": "Donnée (OP_RETURN)", "text": _clean(txt)}


def _tx_row(raw, idx):
    ins, outs, tot = [], [], 0.0
    for o in raw.get("vout", []):
        spk, val = o.get("scriptPubKey") or {}, o.get("value") or 0
        tot += val
        if len(outs) < XP_CAP:
            e = {"v": val, "addr": spk.get("address"), "type": spk.get("type")}
            if spk.get("type") == "nulldata":
                e["data"] = opreturn_text(spk)
            outs.append(e)
    vin = raw.get("vin", [])
    for i in vin[:XP_CAP]:
        if "coinbase" in i:
            ins.append({"cb": True})
        else:
            p = i.get("prevout") or {}
            ins.append({"v": p.get("value"), "addr": (p.get("scriptPubKey") or {}).get("address")})
    vs, fee = raw.get("vsize") or 0, raw.get("fee")
    return {"txid": raw.get("txid"), "i": idx, "cb": bool(vin and "coinbase" in vin[0]), "nin": len(vin),
            "nout": len(raw.get("vout", [])), "out": round(tot, 8), "fee": fee, "vsize": vs,
            "rate": (fee * 1e8 / vs) if (fee is not None and vs) else None, "ins": ins, "outs": outs}


def _flag_mine(rows):
    """Marque les adresses qui sont a toi (elles seront masquees par defaut dans la page)."""
    addrs = []
    for r in rows:
        for e in r["ins"] + r["outs"]:
            a = e.get("addr")
            if a and a not in addrs and len(addrs) < 80:
                addrs.append(a)
    mine = set()
    if addrs:
        res = rpc_batch([("getaddressinfo", [a]) for a in addrs], wallet=WALLET, timeout=20)
        for a, x in zip(addrs, res or []):
            if x and x.get("ismine"):
                mine.add(a)
    for r in rows:
        for e in r["ins"] + r["outs"]:
            if e.get("addr") in mine:
                e["mine"] = True
        r["mine"] = any(e.get("mine") for e in r["outs"])
    return mine


def _raws(bh, ids):
    """Transactions d'un bloc avec frais et entrees (verbosity 2) ; sinon sans (verbosity 1)."""
    if not ids:
        return []
    out = rpc_batch([("getrawtransaction", [t, 2, bh]) for t in ids], timeout=30) or [None] * len(ids)
    miss = [i for i, r in enumerate(out) if r is None]
    if miss:
        again = rpc_batch([("getrawtransaction", [ids[i], 1, bh]) for i in miss], timeout=30) or [None] * len(miss)
        for i, r in zip(miss, again):
            out[i] = r
    return out


def xp_block(ref, page=0, txid=None):
    """Le detail d'un bloc : ref = hauteur (int) ou hash (64 caracteres hexadecimaux)."""
    tip = rpc("getblockcount")
    if tip is None:
        raise XpError("Le nœud ne répond pas pour le moment.")
    if isinstance(ref, int):
        if ref < 0 or ref > tip:
            raise XpError("Le bloc %s n'existe pas encore. Le dernier bloc est le %s." % (fr_int(ref), fr_int(tip)))
        bh = rpc("getblockhash", [ref])
    else:
        bh = ref
    blk = rpc("getblock", [bh, 1], timeout=20) if bh else None
    if not blk:
        raise XpError("Ce bloc est introuvable.")
    txs = blk.get("tx", [])
    n = len(txs)
    pages = max(1, -(-n // XP_PAGE))
    if txid and txid in txs:
        page = txs.index(txid) // XP_PAGE
    page = max(0, min(int(page), pages - 1))
    ids = txs[page * XP_PAGE:(page + 1) * XP_PAGE]
    want = ids if page == 0 else [txs[0]] + ids          # la coinbase sert toujours (recompense, mineur)
    raws = _raws(bh, want)
    cb_raw = raws[0] if raws else None
    rows = []
    for k, r in enumerate(raws[(0 if page == 0 else 1):]):
        i = page * XP_PAGE + k
        rows.append(_tx_row(r, i) if r else {"txid": ids[k], "i": i, "cb": i == 0, "nin": None, "nout": None, "out": None,
                                             "fee": None, "vsize": None, "rate": None, "ins": [], "outs": []})
    cb_row = _tx_row(cb_raw, 0) if cb_raw else None
    _flag_mine(rows + ([cb_row] if (cb_row and page != 0) else []))
    if cb_row is not None and page == 0 and rows:
        cb_row = rows[0]
    stats = rpc("getblockstats", [bh, ["subsidy", "totalfee", "avgfeerate"]], timeout=20) or {}
    cbh = (blk.get("coinbase_tx") or {}).get("coinbase")
    if not cbh and cb_raw and cb_raw.get("vin"):
        cbh = cb_raw["vin"][0].get("coinbase")
    return {"kind": "block", "h": blk.get("height"), "hash": blk.get("hash"), "time": blk.get("time"),
            "conf": blk.get("confirmations"), "ntx": n, "size": blk.get("size"), "weight": blk.get("weight"),
            "diff": blk.get("difficulty"), "ver": blk.get("versionHex"), "bits": blk.get("bits"),
            "nonce": blk.get("nonce"), "merkle": blk.get("merkleroot"), "prev": blk.get("previousblockhash"),
            "next": blk.get("nextblockhash"), "tip": tip, "tag": coinbase_tag(cbh),
            "reward": cb_row["out"] if cb_row else None,
            "subsidy": stats["subsidy"] / 1e8 if "subsidy" in stats else None,
            "fees": stats["totalfee"] / 1e8 if "totalfee" in stats else None,
            "mine": bool(cb_row and cb_row["mine"]), "focus": txid if (txid and txid in txs) else None, "page": page, "pages": pages, "per": XP_PAGE, "rows": rows}


def xp_tx(txid):
    raw = rpc("getrawtransaction", [txid, 2], timeout=15)          # file d'attente, ou index des transactions
    bh = raw.get("blockhash") if raw else None
    if raw is None:
        w = rpc("gettransaction", [txid], wallet=WALLET, timeout=10)       # sinon : une transaction de ton portefeuille
        bh = w.get("blockhash") if w else None
    if bh:
        return xp_block(bh, txid=txid)
    if raw:
        row = _tx_row(raw, None)
        _flag_mine([row])
        ent = rpc("getmempoolentry", [txid]) or {}
        return {"kind": "tx", "row": row, "since": ent.get("time"), "tip": rpc("getblockcount")}
    raise XpError("Rien ne correspond à ces 64 caractères : ce n'est ni un bloc, ni une transaction connue du nœud. "
                  "Une transaction déjà confirmée ne se retrouve que si elle est dans ton portefeuille "
                  "(ou si le nœud tient un index des transactions).")


def xp_parse(q):
    q = (q or "").strip()[:200].lstrip("#").strip()
    d = re.sub(r"[\s  ',]", "", q)
    if re.fullmatch(r"\d{1,9}", d):
        return "h", int(d)
    if re.fullmatch(r"[0-9a-fA-F]{64}", q):
        return "hash", q.lower()
    return None


def xp_search(q):
    p = xp_parse(q)
    if not p:
        if re.match(r"^(rdt1|bc1|tb1|bcrt1|[13])[A-Za-z0-9]{20,}$", (q or "").strip()):
            raise XpError("La recherche par adresse n'existe pas : le nœud ne garde pas d'index des adresses. "
                          "Cherche un numéro de bloc, un hash de bloc ou un identifiant de transaction.")
        raise XpError("Écris un numéro de bloc (par exemple 1234), un hash de bloc ou un identifiant de transaction "
                      "(64 caractères).")
    kind, v = p
    if kind == "h":
        return xp_block(v)
    if rpc("getblockheader", [v]):
        return xp_block(v)
    return xp_tx(v)


_XM = {"n": -1, "items": []}


def xp_mine(page=0, per=20):
    m = wallet_blocks()
    with _W_LOCK:
        if _XM["n"] != len(m):
            _XM["items"] = sorted(m.values(), key=lambda t: t.get("blockheight") or 10 ** 12, reverse=True)
            _XM["n"] = len(m)
        items = _XM["items"]
    total = len(items)
    pages = max(1, -(-total // per))
    page = max(0, min(int(page), pages - 1))
    chunk = items[page * per:(page + 1) * per]
    hds = rpc_batch([("getblockheader", [t["blockhash"]]) for t in chunk], timeout=20) if chunk else []
    rows = []
    for t, hd in zip(chunk, hds or [None] * len(chunk)):
        hd = hd or {}
        rows.append({"hash": t["blockhash"], "h": hd.get("height", t.get("blockheight")), "time": hd.get("time"),
                     "ntx": hd.get("nTx"), "conf": hd.get("confirmations")})
    return {"kind": "mine", "rows": rows, "total": total, "full": bool(FULL_SCAN and _W["scanned"]),
            "page": page, "pages": pages}


def xp_api(path, qs):
    """Les trois routes d'exploration. Toutes en lecture seule, entrees verifiees, une reponse JSON."""
    def one(k, default=""):
        return (qs.get(k) or [default])[0][:200]
    try:
        if not XP_SEM.acquire(timeout=5):
            raise XpError("Beaucoup de demandes en même temps : réessaie dans un instant.")
        try:
            if path == "/api/search":
                return xp_search(one("q"))
            if path == "/api/block":
                p = xp_parse(one("h"))
                if not p:
                    raise XpError("Numéro ou hash de bloc non valide.")
                page = int(one("p", "0")) if re.fullmatch(r"\d{1,6}", one("p", "0")) else 0
                tx = one("tx") if re.fullmatch(r"[0-9a-fA-F]{64}", one("tx")) else None
                return xp_block(p[1], page=page, txid=tx.lower() if tx else None)
            if path == "/api/mine":
                return xp_mine(int(one("p", "0")) if re.fullmatch(r"\d{1,6}", one("p", "0")) else 0)
        finally:
            XP_SEM.release()
        raise XpError("Demande inconnue.")
    except XpError as e:
        return {"kind": "err", "msg": str(e)}
    except Exception:
        return {"kind": "err", "msg": "Impossible de lire cette information pour le moment."}


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
    fk = fork_cached(info)
    if fk:
        secs.append(fk)
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
    adv = advice_cached(info)
    if adv:
        secs.append(adv)
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
                          [[{"tx": t[1], "t": t[1][:12] + "…"}, fnum(t[0], 1), t[2], {"age": t[4]}] for t in txs[:50]]))
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
.fork{display:flex;gap:14px;align-items:flex-start;background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:14px 16px}
.fork .fled{flex:none;width:14px;height:14px;border-radius:50%;margin-top:4px;background:var(--mu)}
.fork.ok .fled{background:var(--good);box-shadow:0 0 10px var(--good)}
.fork.warn{border-color:var(--warn)}.fork.warn .fled{background:var(--warn);box-shadow:0 0 10px var(--warn);animation:blink 1.6s infinite}
.fork.bad{border-color:var(--bad)}.fork.bad .fled{background:var(--bad);box-shadow:0 0 10px var(--bad);animation:blink 1.1s infinite}
.fork b{display:block;font-size:17px;font-weight:800}
.fork.ok b{color:var(--good)}.fork.warn b{color:var(--warn)}.fork.bad b{color:var(--bad)}
.fork p{margin:4px 0 0;font-size:13px;color:var(--mu);line-height:1.45}
.fork ul{margin:9px 0 0;padding-left:18px;font-size:13px;line-height:1.5;color:var(--tx)}
.fork li{margin:3px 0}
.crow .l.w{width:170px;text-transform:none;letter-spacing:0;font-size:13px}
@media(max-width:640px){.crow .l.w{width:100%}}
.srch{flex:1 1 300px;max-width:480px;display:flex;gap:8px;order:2}
.top .brand{order:1}.top .status{order:3}
.srch input{flex:1;min-width:0;background:#0e1626;color:var(--tx);border:1px solid var(--edge);border-radius:10px;padding:9px 12px;font:14px Inter,system-ui,sans-serif}
.srch input:focus{outline:2px solid var(--acc);outline-offset:1px;border-color:var(--acc)}
.srch button{background:var(--acc);color:#1a0f08;border:0;border-radius:10px;padding:9px 14px;font:700 13px Inter,system-ui,sans-serif;cursor:pointer}
.srch button:hover{background:var(--acc2)}
@media(max-width:900px){.srch{order:3;flex:1 1 100%;max-width:none}.top .status{order:2}}
.blk{cursor:pointer;transition:transform .15s,border-color .2s}
.blk:hover,.blk:focus-visible{transform:translateY(-3px);border-color:var(--acc);outline:none}
.card.click{cursor:pointer}.card.click:hover{border-color:var(--acc)}
button.xlink{background:none;border:0;color:var(--acc2);font:inherit;cursor:pointer;padding:0;text-decoration:underline;text-underline-offset:3px}
button.xlink.mono,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}
body.xpopen{overflow:hidden}
.xp{position:fixed;inset:0;z-index:50;background:rgba(5,8,14,.82);overflow:auto;padding:18px 12px;display:flex;justify-content:center;align-items:flex-start}
.xp[hidden]{display:none}
.xpc{width:min(980px,100%);background:var(--bg2);border:1px solid var(--edge);border-radius:16px;padding:0 0 18px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
.xpbar{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--edge);position:sticky;top:0;background:var(--bg2);border-radius:16px 16px 0 0;z-index:2}
.xpt{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--mu)}
button.xpx{background:#0e1626;color:var(--tx);border:1px solid var(--edge);border-radius:8px;padding:6px 12px;font:600 12px Inter,system-ui,sans-serif;cursor:pointer}
.xpb{padding:14px 16px 0}
.xpmsg{padding:26px 8px;text-align:center;color:var(--mu);font-size:15px;line-height:1.5}.xpmsg.bad{color:var(--bad)}
.xpnav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
button.xpb2{background:#0e1626;color:var(--tx);border:1px solid var(--edge);border-radius:8px;padding:7px 12px;font:600 12px Inter,system-ui,sans-serif;cursor:pointer}
button.xpb2:hover:not(:disabled){border-color:var(--acc);color:var(--acc2)}
button.xpb2:disabled{opacity:.35;cursor:default}
.xtitle{display:flex;align-items:center;flex-wrap:wrap;gap:8px 10px;margin:4px 0 12px}
.xtitle h2{margin:0;font-size:26px;font-weight:800;letter-spacing:-.01em}
.xbadge{font-size:11px;font-weight:700;border:1px solid var(--edge);border-radius:999px;padding:3px 9px;color:var(--mu);background:var(--card)}
.xbadge.mine{color:var(--acc2);border-color:var(--acc);background:#2a1a12}.xbadge.cb{color:var(--good);border-color:var(--good)}
.xbadge.bad{color:var(--bad);border-color:var(--bad)}.xbadge.warn{color:var(--warn);border-color:var(--warn)}.xbadge.conf{color:var(--good)}
.xkvs{background:var(--card);border:1px solid var(--edge);border-radius:12px;padding:4px 14px;margin-bottom:10px}
.xkv{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid rgba(46,58,80,.55);font-size:14px;align-items:baseline}
.xkv:last-child{border-bottom:0}
.xl{flex:none;width:150px;color:var(--mu);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.xvv{min-width:0;overflow-wrap:anywhere;line-height:1.45}
.xvv code{font-size:12px;word-break:break-all}
details.xdet{margin-bottom:14px}details.xdet summary{cursor:pointer;color:var(--mu);font-size:13px;margin:0 0 8px}
.xhead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:16px 0 8px}
.xhead.foot{margin-top:10px;justify-content:flex-end}
.xhead h3{margin:0;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--mu)}
.xppg{display:flex;align-items:center;gap:10px;font-size:12px}
.xlist{display:flex;flex-direction:column;gap:6px}
.xtx{background:var(--card);border:1px solid var(--edge);border-radius:10px;overflow:hidden}
.xtx.open{border-color:var(--acc)}
.xth{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px;padding:10px 12px;cursor:pointer;font-size:13px}
.xth:hover,.xth:focus-visible{background:#1b2538;outline:none}
.xi{color:var(--mu);min-width:30px;font-variant-numeric:tabular-nums}
.xid{font-weight:600}
.xt2{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums}
.xtd{padding:2px 12px 12px;border-top:1px solid var(--edge);background:#121a29}
.xfull{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:10px 0;font-size:12px}
.xfull code{overflow-wrap:anywhere;word-break:break-all}
.xiog{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:700px){.xiog{grid-template-columns:1fr}.xl{width:100%}.xkv{flex-wrap:wrap;gap:2px}.xt2{margin-left:0}}
.xio h4{margin:6px 0;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--mu)}
.xir{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px dashed rgba(46,58,80,.6);font-size:12px}
.xa{min-width:0;overflow-wrap:anywhere;word-break:break-all}.xv{flex:none;font-variant-numeric:tabular-nums;font-weight:600}
button.xmine{background:#2a1a12;color:var(--acc2);border:1px solid var(--acc);border-radius:6px;padding:2px 8px;font:600 11px Inter,system-ui,sans-serif;cursor:pointer}
.xdata{margin:4px 0 6px;padding:7px 9px;background:#0e1626;border-left:3px solid var(--acc);border-radius:6px}
.xdata small{display:block;color:var(--mu);font-size:10px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:3px}
.xq{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}
.xmore,.xsz{font-size:12px;margin-top:6px}
.xnote{font-size:12px;margin:14px 0 2px;line-height:1.5}
.note{margin-top:22px;padding:12px 16px;border-radius:12px;border:1px solid var(--edge);background:var(--card);font-size:14px}
.note.bad{border-color:var(--bad);color:var(--bad)}.note.warn{border-color:var(--warn);color:var(--warn)}
footer{margin-top:34px;color:#6b7890;font-size:12px;text-align:center}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style></head><body><div class="wrap">
<header class="top">
 <div class="brand">
  <svg viewBox="0 0 256 256" aria-hidden="true"><defs><linearGradient id="rdc-rimk" x1="0.1" y1="0" x2="0.9" y2="1"><stop offset="0" stop-color="#0055A4"/><stop offset="0.3" stop-color="#0055A4"/><stop offset="0.65" stop-color="#0055A4"/><stop offset="1" stop-color="#0055A4"/></linearGradient><radialGradient id="rdc-facek" cx="50%" cy="42%" r="70%"><stop offset="0" stop-color="#050b17"/><stop offset="0.55" stop-color="#050b17"/><stop offset="1" stop-color="#050b17"/></radialGradient><linearGradient id="rdc-lgk" gradientUnits="userSpaceOnUse" x1="0" y1="62" x2="0" y2="194"><stop offset="0" stop-color="#0055A4"/><stop offset="0.35" stop-color="#0055A4"/><stop offset="0.7" stop-color="#0055A4"/><stop offset="1" stop-color="#0055A4"/></linearGradient><radialGradient id="rdc-shinek" cx="30%" cy="22%" r="55%"><stop offset="0" stop-color="#fff" stop-opacity="0.28"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient><filter id="rdc-shk" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="3.2"/></filter><clipPath id="rdc-clipk"><circle cx="128" cy="128" r="106"/></clipPath><path id="rdc-R" d="M75 77.13 88.2 78.3C88.49 93.4 88.49 108.5 88.49 123.75V130.2C88.49 145.45 88.49 160.55 88.2 175.36L75 176.67V181.66H126.9V176.67L113.41 175.36C112.97 160.4 112.97 145.3 112.97 129.91H120.01C131.3 129.91 134.82 135.77 136.87 147.79L141.12 168.02C142.59 179.9 150.07 183.86 163.55 183.86C170.88 183.86 174.99 183.13 181 181.66V176.67L167.95 175.8L163.41 151.75C160.91 137.24 155.64 129.03 136.43 126.53C159.59 123.02 167.66 111.73 167.66 99.7C167.66 82.26 153.58 72.14 127.93 72.14H75ZM113.41 77.71H121.62C136.43 77.71 144.2 85.63 144.2 100.14C144.2 113.49 137.9 124.48 121.48 124.48H112.97C112.97 107.62 112.97 92.52 113.41 77.71Z"/></defs><circle cx="128" cy="128" r="124" fill="url(#rdc-rimk)"/><circle cx="128" cy="128" r="108.2" fill="#02122a"/><circle cx="128" cy="128" r="106" fill="url(#rdc-facek)"/><circle cx="128" cy="34" r="3.7" fill="#8F9195"/><circle cx="140.27" cy="34.8" r="3.7" fill="#8F9195"/><circle cx="152.33" cy="37.2" r="3.7" fill="#8F9195"/><circle cx="163.97" cy="41.16" r="3.7" fill="#8F9195"/><circle cx="175" cy="46.59" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="175" cy="46.59" r="2.4" fill="#EF4135"/><circle cx="185.22" cy="53.42" r="3.7" fill="#8F9195"/><circle cx="194.47" cy="61.53" r="3.7" fill="#8F9195"/><circle cx="202.58" cy="70.78" r="3.7" fill="#8F9195"/><circle cx="209.41" cy="81" r="3.7" fill="#8F9195"/><circle cx="214.84" cy="92.03" r="3.7" fill="#8F9195"/><circle cx="218.8" cy="103.67" r="3.7" fill="#8F9195"/><circle cx="221.2" cy="115.73" r="3.7" fill="#8F9195"/><circle cx="222" cy="128" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="222" cy="128" r="2.4" fill="#EF4135"/><circle cx="221.2" cy="140.27" r="3.7" fill="#8F9195"/><circle cx="218.8" cy="152.33" r="3.7" fill="#8F9195"/><circle cx="214.84" cy="163.97" r="3.7" fill="#8F9195"/><circle cx="209.41" cy="175" r="3.7" fill="#8F9195"/><circle cx="202.58" cy="185.22" r="3.7" fill="#8F9195"/><circle cx="194.47" cy="194.47" r="3.7" fill="#8F9195"/><circle cx="185.22" cy="202.58" r="3.7" fill="#8F9195"/><circle cx="175" cy="209.41" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="175" cy="209.41" r="2.4" fill="#EF4135"/><circle cx="163.97" cy="214.84" r="3.7" fill="#8F9195"/><circle cx="152.33" cy="218.8" r="3.7" fill="#8F9195"/><circle cx="140.27" cy="221.2" r="3.7" fill="#8F9195"/><circle cx="128" cy="222" r="3.7" fill="#8F9195"/><circle cx="115.73" cy="221.2" r="3.7" fill="#8F9195"/><circle cx="103.67" cy="218.8" r="3.7" fill="#8F9195"/><circle cx="92.03" cy="214.84" r="3.7" fill="#8F9195"/><circle cx="81" cy="209.41" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="81" cy="209.41" r="2.4" fill="#EF4135"/><circle cx="70.78" cy="202.58" r="3.7" fill="#8F9195"/><circle cx="61.53" cy="194.47" r="3.7" fill="#8F9195"/><circle cx="53.42" cy="185.22" r="3.7" fill="#8F9195"/><circle cx="46.59" cy="175" r="3.7" fill="#8F9195"/><circle cx="41.16" cy="163.97" r="3.7" fill="#8F9195"/><circle cx="37.2" cy="152.33" r="3.7" fill="#8F9195"/><circle cx="34.8" cy="140.27" r="3.7" fill="#8F9195"/><circle cx="34" cy="128" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="34" cy="128" r="2.4" fill="#EF4135"/><circle cx="34.8" cy="115.73" r="3.7" fill="#8F9195"/><circle cx="37.2" cy="103.67" r="3.7" fill="#8F9195"/><circle cx="41.16" cy="92.03" r="3.7" fill="#8F9195"/><circle cx="46.59" cy="81" r="3.7" fill="#8F9195"/><circle cx="53.42" cy="70.78" r="3.7" fill="#8F9195"/><circle cx="61.53" cy="61.53" r="3.7" fill="#8F9195"/><circle cx="70.78" cy="53.42" r="3.7" fill="#8F9195"/><circle cx="81" cy="46.59" r="7.4" fill="#02122a" stroke="#EF4135" stroke-width="3.2"/><circle cx="81" cy="46.59" r="2.4" fill="#EF4135"/><circle cx="92.03" cy="41.16" r="3.7" fill="#8F9195"/><circle cx="103.67" cy="37.2" r="3.7" fill="#8F9195"/><circle cx="115.73" cy="34.8" r="3.7" fill="#8F9195"/><use href="#rdc-R" transform="translate(0 4)" fill="#000" fill-opacity="0.6" filter="url(#rdc-shk)"/><use href="#rdc-R" transform="translate(2.2 3)" fill="#003a75"/><use href="#rdc-R" fill="url(#rdc-lgk)"/></svg>
  <div><h1 id="ttl">__TITLE__</h1><p class="sub" id="sub"></p></div>
 </div>
 <form class="srch" id="srch" role="search" autocomplete="off"><input id="q" type="search" placeholder="N° de bloc, hash ou transaction" aria-label="Rechercher un bloc ou une transaction" maxlength="130" enterkeyhint="search"><button type="submit">Chercher</button></form>
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
  if(it.id==="found"){c.el.classList.add("click");c.el.onclick=()=>xpGo("mes-blocs");c.el.title="Voir mes blocs trouvés";}
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
   e.tabIndex=0;e.setAttribute("role","button");{const go=()=>xpGo("bloc/"+b.hash);e.onclick=go;e.onkeydown=ev=>{if(ev.key==="Enter"||ev.key===" "){ev.preventDefault();go();}};}
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
 if(last){{const lb=$("button","xlink",fmt(last.h,0));lb.type="button";lb.style.fontWeight="800";lb.title="Voir le contenu de ce bloc";lb.onclick=()=>xpGo("bloc/"+last.hash);rec.foot.append("Dernier bloc : ",lb,", trouvé il y a ",ageSpan(last.time));}}
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
 if(c&&typeof c==="object"){ if(c.age)td.append(ageSpan(c.age)); else if(c.tx){const a=$("button","xlink mono",c.t);a.type="button";a.title="Voir cette transaction";a.onclick=()=>xpGo("tx/"+c.tx);td.append(a);} else{td.textContent=c.t;if(c.cls)td.className=c.cls;} }
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
R.fork=(rec,d)=>{
 if(!rec.el){rec.el=$("section","sec");rec.el.append($("h2","","Surveillance des forks"));
  rec.box=$("div","fork");rec.led=$("i","fled");const body=$("div","");
  rec.t=$("b");rec.p=$("p");rec.ul=$("ul");body.append(rec.t,rec.p,rec.ul);rec.box.append(rec.led,body);rec.el.append(rec.box);}
 rec.box.className="fork "+d.state;rec.t.textContent=d.label;rec.p.textContent=d.detail;
 const sg=JSON.stringify(d.lines);
 if(rec.sig!==sg){rec.sig=sg;rec.ul.textContent="";d.lines.forEach(x=>rec.ul.append($("li","",x)));}
 rec.ul.style.display=d.lines.length?"":"none";
};
function copyTxt(v,b){
 const ok=()=>{b.textContent="Copié ✓";setTimeout(()=>b.textContent="Copier",1500);};
 if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(v).then(ok,()=>{});
 else{const t=$("textarea");t.value=v;document.body.append(t);t.select();try{document.execCommand("copy");ok();}catch(e){}t.remove();}
}
R.advice=(rec,d)=>{
 if(!rec.el){rec.el=$("section","sec");rec.el.append($("h2","",d.title));rec.box=$("div","conn");rec.rows=[];
  d.items.forEach((it,i)=>{const r=$("div","crow"),c=$("code");r.append($("span","l w",it.label),c);
   const row={c,v:""};
   if(it.copy){const b=$("button","cp","Copier");b.onclick=()=>copyTxt(row.v,b);r.append(b);}
   rec.box.append(r);rec.rows.push(row);});
  rec.note=$("div","cwarn");rec.box.append(rec.note);rec.el.append(rec.box);}
 d.items.forEach((it,i)=>{const row=rec.rows[i];row.v=it.value;if(row.c.textContent!==it.value)row.c.textContent=it.value;});
 rec.note.textContent=d.warn||"";
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

/* ---- explorateur de blocs : tout ce qui vient de la chaine est affiche en texte brut (textContent), jamais en HTML */
const XP={el:null,body:null,seq:0};
const trimNum=(v,d)=>{if(v==null)return "-";let s=Number(v).toFixed(d==null?8:d);if(s.indexOf(".")>=0)s=s.replace(/0+$/,"").replace(/\.$/,"");return Number(s).toLocaleString("fr-FR",{maximumFractionDigits:d==null?8:d});};
const rdt=v=>v==null?"-":trimNum(v,8)+" RDT";
const bytesTxt=n=>n==null?"-":(n<1024?n+" o":(n<1048576?(n/1024).toFixed(1).replace(".",",")+" Ko":(n/1048576).toFixed(2).replace(".",",")+" Mo"));
const diffTxt=d=>d==null?"-":(d<1?Number(d).toPrecision(3).replace(".",","):fmt(d,2));
const shortId=t=>t?t.slice(0,10)+"…"+t.slice(-6):"-";
function xpGo(x){const t="#"+x;if(location.hash===t)xpRoute();else location.hash=t;}
function xpHide(){if(XP.el)XP.el.hidden=true;document.body.classList.remove("xpopen");XP.seq++;}
function xpClose(){if(location.hash)history.pushState(null,"",location.pathname+location.search);xpHide();}
function xpShell(){
 if(XP.el)return;
 const ov=$("div","xp");ov.hidden=true;ov.setAttribute("role","dialog");ov.setAttribute("aria-modal","true");ov.setAttribute("aria-label","Explorateur de blocs");
 const card=$("div","xpc"),bar=$("div","xpbar"),x=$("button","xpx","✕ Fermer");card.tabIndex=-1;XP.card=card;x.type="button";x.onclick=xpClose;
 bar.append($("span","xpt","Explorateur de blocs"),x);XP.body=$("div","xpb");card.append(bar,XP.body);ov.append(card);
 ov.addEventListener("click",e=>{if(e.target===ov)xpClose();});document.body.append(ov);XP.el=ov;
 document.addEventListener("keydown",e=>{if(e.key==="Escape"&&!ov.hidden)xpClose();});
}
function xpRoute(){
 let raw;try{raw=decodeURIComponent(location.hash.replace(/^#/,""));}catch(e){xpHide();return;}
 const m=raw.split("/");
 let url=null;
 if(m[0]==="bloc"&&m[1])url="api/block?h="+encodeURIComponent(m[1])+(m[2]?"&p="+encodeURIComponent(m[2]):"");
 else if(m[0]==="tx"&&m[1])url="api/search?q="+encodeURIComponent(m[1]);
 else if(m[0]==="mes-blocs")url="api/mine?p="+encodeURIComponent(m[1]||"0");
 else if(m[0]==="chercher"&&m.length>1)url="api/search?q="+encodeURIComponent(m.slice(1).join("/"));
 if(!url){xpHide();return;}
 xpLoad(url);
}
async function xpLoad(url){
 xpShell();const my=++XP.seq;XP.el.hidden=false;document.body.classList.add("xpopen");try{XP.card.focus({preventScroll:true});}catch(e){}
 XP.body.textContent="";XP.body.append($("div","xpmsg","Chargement…"));
 try{const r=await fetch(url,{cache:"no-store"});const d=await r.json();if(my!==XP.seq)return;xpRender(d);XP.el.scrollTop=0;}
 catch(e){if(my!==XP.seq)return;XP.body.textContent="";XP.body.append($("div","xpmsg bad","Page injoignable. Réessaie dans un instant."));}
}
function xpRender(d){
 XP.body.textContent="";
 if(d.kind==="block")xpBlock(d);else if(d.kind==="tx")xpTx(d);else if(d.kind==="mine")xpMine(d);
 else XP.body.append($("div","xpmsg bad",d.msg||"Rien à afficher."));
}
function xpPager(page,pages,go){
 const p=$("div","xppg"),a=$("button","xpb2","◀"),b=$("button","xpb2","▶");a.type=b.type="button";
 a.disabled=page<=0;b.disabled=page>=pages-1;a.onclick=()=>go(page-1);b.onclick=()=>go(page+1);
 p.append(a,$("span","mu","page "+(page+1)+" sur "+pages),b);return p;
}
function addrNode(e){
 const s=$("span","xa");
 if(!e.addr){s.textContent=e.type==="nulldata"?"donnée OP_RETURN":"script sans adresse";s.classList.add("mu");return s;}
 if(e.mine){const b=$("button","xmine","Ton adresse · afficher");b.type="button";b.onclick=ev=>{ev.stopPropagation();s.textContent=e.addr;s.classList.add("shown");};s.append(b);return s;}
 s.textContent=e.addr;return s;
}
function ioList(title,list,total){
 const c=$("div","xio");c.append($("h4","",title+" ("+(total==null?"?":total)+")"));
 list.forEach(e=>{
  const r=$("div","xir");
  if(e.cb){r.append($("span","xa mu","Nouveaux RDT (récompense du bloc)"));}
  else{r.append(addrNode(e));if(e.v!=null)r.append($("span","xv",rdt(e.v)));}
  c.append(r);
  if(e.data){const q=$("div","xdata");q.append($("small","",e.data.label),$("div","xq",e.data.text||"(vide)"));c.append(q);}
 });
 if(total!=null&&total>list.length)c.append($("div","mu xmore","… et "+(total-list.length)+" autre(s) non affichée(s)"));
 return c;
}
function txRow(r,open){
 const w=$("div","xtx"),h=$("div","xth");h.tabIndex=0;h.setAttribute("role","button");
 h.append($("span","xi",r.i==null?"":"#"+r.i),$("span","xid mono",shortId(r.txid)));
 if(r.cb)h.append($("span","xbadge cb","Coinbase"));
 if(r.mine)h.append($("span","xbadge mine","ton portefeuille"));
 const m=$("span","xm mu",(r.nin==null?"?":r.nin)+" → "+(r.nout==null?"?":r.nout));h.append(m);
 h.append($("span","xt2",r.out==null?"":rdt(r.out)));
 if(!r.cb&&r.fee!=null)h.append($("span","xf mu","frais "+trimNum(r.fee*1e8,0)+" sat"+(r.rate!=null?" · "+trimNum(r.rate,1)+" sat/vB":"")));
 const d=$("div","xtd");d.hidden=!open;
 const full=$("div","xfull");full.append($("span","mu","Identifiant : "),$("code","",r.txid||"-"));
 if(r.txid){const cp=$("button","cp","Copier");cp.type="button";cp.onclick=ev=>{ev.stopPropagation();copyTxt(r.txid,cp);};full.append(cp);}
 d.append(full);
 const g=$("div","xiog");g.append(ioList("Entrées",r.ins,r.nin),ioList("Sorties",r.outs,r.nout));d.append(g);
 if(r.vsize)d.append($("div","mu xsz","Taille : "+r.vsize+" vB"));
 const tog=()=>{d.hidden=!d.hidden;w.classList.toggle("open",!d.hidden);};
 h.onclick=tog;h.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();tog();}};
 w.classList.toggle("open",!!open);w.append(h,d);return w;
}
function kvx(label,node){const r=$("div","xkv");r.append($("span","xl",label));const v=$("span","xvv");if(typeof node==="string")v.textContent=node;else v.append(node);r.append(v);return r;}
function hashLine(hx){const s=$("span","");s.append($("code","mono",hx));const cp=$("button","cp","Copier");cp.type="button";cp.onclick=()=>copyTxt(hx,cp);s.append(" ",cp);return s;}
function xpNav(d){
 const n=$("div","xpnav");
 const pv=$("button","xpb2","◀ Bloc précédent"),nx=$("button","xpb2","Bloc suivant ▶"),tp=$("button","xpb2","Dernier bloc"),mb=$("button","xpb2","Mes blocs trouvés");
 [pv,nx,tp,mb].forEach(b=>b.type="button");
 pv.disabled=!d.prev;nx.disabled=!d.next;tp.disabled=d.h===d.tip&&d.conf>0;
 pv.onclick=()=>xpGo("bloc/"+d.prev);nx.onclick=()=>xpGo("bloc/"+d.next);tp.onclick=()=>xpGo("bloc/"+d.tip);mb.onclick=()=>xpGo("mes-blocs");
 n.append(pv,nx,tp,mb);return n;
}
function xpBlock(d){
 const b=XP.body;
 b.append(xpNav(d));
 const t=$("div","xtitle");t.append($("h2","","Bloc "+fmt(d.h,0)));
 if(d.mine)t.append($("span","xbadge mine","Trouvé par toi"));
 if(d.conf<0)t.append($("span","xbadge bad","Bloc orphelin (hors de la chaîne principale)"));
 else if(d.h===d.tip)t.append($("span","xbadge","Dernier bloc"));
 if(d.conf>=0)t.append($("span","xbadge conf",d.conf+(d.conf>1?" confirmations":" confirmation")));
 b.append(t);
 const kv=$("div","xkvs");
 kv.append(kvx("Date",new Date(d.time*1000).toLocaleString("fr-FR")+" (il y a "+ago(d.time)+")"));
 kv.append(kvx("Hash",hashLine(d.hash)));
 kv.append(kvx("Transactions",String(d.ntx)));
 kv.append(kvx("Taille",bytesTxt(d.size)+" · poids "+fmt(d.weight||0,0)+" WU"));
 if(d.reward!=null){let s=rdt(d.reward);if(d.subsidy!=null&&d.fees!=null)s+=" (subvention "+trimNum(d.subsidy,8)+" + frais "+trimNum(d.fees,8)+")";kv.append(kvx("Récompense",s));}
 kv.append(kvx("Difficulté",diffTxt(d.diff)));
 if(d.tag)kv.append(kvx("Étiquette du mineur",d.tag));
 if(d.prev&&d.h>0){const a=$("button","xlink mono",shortId(d.prev));a.type="button";a.onclick=()=>xpGo("bloc/"+d.prev);kv.append(kvx("Bloc précédent",a));}
 b.append(kv);
 const det=$("details","xdet");det.append($("summary","","Détails techniques"));
 const k2=$("div","xkvs");
 k2.append(kvx("Version",d.ver||"-"),kvx("Bits",d.bits||"-"),kvx("Nonce",String(d.nonce)),kvx("Racine de Merkle",d.merkle||"-"));
 det.append(k2);b.append(det);
 const head=$("div","xhead");head.append($("h3","","Transactions ("+fmt(d.ntx,0)+")"));
 if(d.pages>1)head.append(xpPager(d.page,d.pages,p=>xpGo("bloc/"+(d.conf<0?d.hash:d.h)+"/"+p)));
 b.append(head);
 const list=$("div","xlist");
 d.rows.forEach(r=>list.append(txRow(r,(d.focus&&r.txid===d.focus)||(d.rows.length===1&&d.ntx===1))));
 b.append(list);
 if(d.pages>1){const f=$("div","xhead foot");f.append(xpPager(d.page,d.pages,p=>xpGo("bloc/"+(d.conf<0?d.hash:d.h)+"/"+p)));b.append(f);}
 b.append($("div","mu xnote","Tes propres adresses sont masquées : clique sur « Ton adresse » pour les afficher. Tout ce qui est écrit dans une transaction ou dans l'étiquette du mineur vient de la chaîne : c'est du texte libre, non vérifié."));
 history.replaceState(null,"","#bloc/"+(d.conf<0?d.hash:d.h)+(d.page?"/"+d.page:""));
}
function xpTx(d){
 const b=XP.body,n=$("div","xpnav");
 const tp=$("button","xpb2","Dernier bloc");tp.type="button";tp.onclick=()=>xpGo("bloc/"+d.tip);n.append(tp);b.append(n);
 const t=$("div","xtitle");t.append($("h2","","Transaction en attente"),$("span","xbadge warn","pas encore dans un bloc"));b.append(t);
 if(d.since)b.append($("div","mu","Dans la file d'attente depuis "+ago(d.since)+"."));
 const list=$("div","xlist");list.append(txRow(d.row,true));b.append(list);
}
function xpMine(d){
 const b=XP.body,n=$("div","xpnav"),tp=$("button","xpb2","Dernier bloc");tp.type="button";
 tp.onclick=()=>{fetch("api/state",{cache:"no-store"}).then(r=>r.json()).then(s=>{const c=s.sections.find(x=>x.type==="chain");if(c&&c.blocks.length)xpGo("bloc/"+c.blocks[c.blocks.length-1].hash);});};
 n.append(tp);b.append(n);
 const t=$("div","xtitle");t.append($("h2","","Mes blocs trouvés"),$("span","xbadge mine",fmt(d.total,0)+(d.full?"":" récents")));b.append(t);
 if(!d.full)b.append($("div","mu xnote","Cette application ne montre que tes blocs les plus récents. La liste complète est dans le Tableau de bord."));
 if(!d.rows.length){b.append($("div","xpmsg","Aucun bloc trouvé par ton portefeuille pour le moment."));return;}
 const list=$("div","xlist");
 d.rows.forEach(r=>{
  const w=$("div","xtx"),h=$("div","xth");h.tabIndex=0;h.setAttribute("role","button");
  h.append($("span","xid","Bloc "+(r.h==null?"?":fmt(r.h,0))));
  if(r.time)h.append($("span","xm mu","il y a "+ago(r.time)));
  if(r.ntx!=null)h.append($("span","xt2",r.ntx+" tx"));
  if(r.conf!=null)h.append($("span","xf mu",r.conf+" conf."));
  const go=()=>xpGo("bloc/"+r.hash);h.onclick=go;h.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();go();}};
  w.append(h);list.append(w);
 });
 b.append(list);
 if(d.pages>1){const f=$("div","xhead foot");f.append(xpPager(d.page,d.pages,p=>xpGo("mes-blocs/"+p)));b.append(f);}
 history.replaceState(null,"","#mes-blocs"+(d.page?"/"+d.page:""));
}
(function(){
 const f=document.getElementById("srch");
 f.addEventListener("submit",e=>{e.preventDefault();const q=document.getElementById("q").value.trim();if(!q)return;xpGo("chercher/"+encodeURIComponent(q));});
 window.addEventListener("hashchange",xpRoute);
 if(location.hash)xpRoute();
})();

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
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        sp = urllib.parse.urlsplit(self.path)
        path = sp.path
        if path in ("/api/search", "/api/block", "/api/mine"):
            self._send(200, json.dumps(xp_api(path, urllib.parse.parse_qs(sp.query[:600])), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(dict(get_state(), ui=UI_VER), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send(200, page(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=wallet_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
