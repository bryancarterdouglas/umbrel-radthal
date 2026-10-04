import base64, glob, json, os, time, urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIEW = os.environ.get("VIEW", "dashboard")
RPC = os.environ.get("RPC", "http://radthal-host:9332")
RPCUSER = os.environ.get("RPCUSER", "radthal")
PASSFILE = os.environ.get("PASSFILE", "/run/secrets/radthal_rpc_password")
LOGS = os.environ.get("LOGS", "/ckpool/logs")
PORT = int(os.environ.get("PORT", "8000"))
WALLET = os.environ.get("WALLET", "minage")
STRATUM = os.environ.get("STRATUM_PORT", "3333")
TITLES = {"node": "Noeud Radthal", "dashboard": "Tableau de bord Radthal",
          "mempool": "Mempool Radthal", "ckpool": "ckpool Radthal"}

CSS = """body{font-family:system-ui,sans-serif;background:#12141a;color:#e8e8ea;margin:0;padding:16px;max-width:960px;margin:auto}
h1{font-size:22px;margin:4px 0} h2{font-size:16px;color:#9db4ff;margin:22px 0 6px} h3{font-size:13px;word-break:break-all}
table{width:100%;border-collapse:collapse;background:#1c1f28;border-radius:8px;margin-bottom:8px}
td,th{padding:6px 10px;border-bottom:1px solid #2a2e3a;font-size:13px;text-align:left;word-break:break-all}
th{color:#9db4ff;font-weight:600} td:first-child{color:#aab}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.card{background:#1c1f28;border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:20px;margin-top:4px} .card span{font-size:12px;color:#aab}
.w{margin-left:12px;width:calc(100% - 12px)} small,.muted{color:#889;font-size:12px}
code{background:#252a36;padding:2px 6px;border-radius:4px;word-break:break-all}"""


def rpc(method, params=None, wallet=None):
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        url = RPC + ("/wallet/" + wallet if wallet else "")
        body = json.dumps({"jsonrpc": "1.0", "id": "u", "method": method, "params": params or []}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "Authorization": auth})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)["result"]
    except Exception:
        return None


def hs(x):
    try:
        x = float(x)
    except Exception:
        return "-"
    for u in ["H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s"]:
        if x < 1000:
            return "%.2f %s" % (x, u)
        x /= 1000
    return "%.2f EH/s" % x


def ago(ts):
    try:
        d = max(0, int(time.time() - float(ts)))
    except Exception:
        return "-"
    if d < 60:
        return "%d s" % d
    if d < 3600:
        return "%d min" % (d // 60)
    if d < 86400:
        return "%d h %d min" % (d // 3600, d % 3600 // 60)
    return "%d j %d h" % (d // 86400, d % 86400 // 3600)


def size(n):
    try:
        n = float(n)
    except Exception:
        return "-"
    for u in ["o", "Ko", "Mo", "Go"]:
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024
    return "%.1f To" % n


def kv(pairs):
    return "<table>" + "".join("<tr><td>%s</td><td>%s</td></tr>" % (escape(str(k)), escape(str(v))) for k, v in pairs) + "</table>"


def table(head, rows):
    h = "".join("<th>%s</th>" % escape(c) for c in head)
    b = "".join("<tr>" + "".join("<td>%s</td>" % escape(str(c)) for c in r) + "</tr>" for r in rows)
    return "<table><tr>%s</tr>%s</table>" % (h, b)


def cards(items):
    return "<div class='cards'>" + "".join("<div class='card'><span>%s</span><b>%s</b></div>" % (escape(str(k)), escape(str(v))) for k, v in items) + "</div>"


def sec(title, inner):
    return "<h2>%s</h2>%s" % (escape(title), inner)


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


def ckpool_users():
    res = []
    for f in sorted(glob.glob(LOGS + "/users/*")):
        try:
            res.append((os.path.basename(f), json.load(open(f))))
        except Exception:
            pass
    return res


def scalars(d):
    return [(k, v) for k, v in d.items() if not isinstance(v, (list, dict))]


def recent_blocks(h, n=10):
    out = []
    if h is None:
        return out
    for i in range(h, max(h - n, -1), -1):
        bh = rpc("getblockhash", [i])
        b = rpc("getblock", [bh]) if bh else None
        if b:
            out.append(b)
    return out


def blocks_table(blocks):
    return table(["Hauteur", "Tx", "Taille", "Il y a", "Hash"],
                 [(b.get("height"), b.get("nTx"), size(b.get("size")), ago(b.get("time")), str(b.get("hash", ""))[:16] + "...") for b in blocks])


def view_node():
    info = rpc("getblockchaininfo") or {}
    net = rpc("getnetworkinfo") or {}
    peers = rpc("getpeerinfo") or []
    up = rpc("uptime")
    warn = info.get("warnings")
    if isinstance(warn, list):
        warn = " ; ".join(warn)
    s = sec("Etat du noeud", kv([
        ("Etat", "en marche" if info else "NE REPOND PAS"),
        ("Reseau", info.get("chain", "-")),
        ("Blocs", info.get("blocks", "-")),
        ("En-tetes", info.get("headers", "-")),
        ("Difficulte", info.get("difficulty", "-")),
        ("Taille sur disque", size(info.get("size_on_disk"))),
        ("Version", net.get("subversion", "-")),
        ("Connexions", net.get("connections", "-")),
        ("Temps de fonctionnement", ago(time.time() - up) if up else "-"),
        ("Avertissements", warn or "aucun"),
    ]))
    s += sec("Pairs connectes", table(["Adresse", "Version", "Sens", "Recu", "Envoye", "Connecte depuis"], [
        (p.get("addr"), p.get("subver"), "entrant" if p.get("inbound") else "sortant", size(p.get("bytesrecv")), size(p.get("bytessent")), ago(p.get("conntime"))) for p in peers]) if peers else "<p class='muted'>Aucun pair connecte.</p>")
    s += sec("Ports", kv([("P2P (reseau)", "9333"), ("RPC (local)", "9332"), ("Stratum (mineurs)", STRATUM)]))
    return s


def view_dashboard():
    info = rpc("getblockchaininfo") or {}
    mp = rpc("getmempoolinfo") or {}
    net = rpc("getnetworkinfo") or {}
    bal = (rpc("getbalances", wallet=WALLET) or {}).get("mine", {})
    nh = rpc("getnetworkhashps")
    st = ckpool_status()
    h = info.get("blocks")
    blocks = recent_blocks(h)
    left = "-" if h is None else 2016 - (h + 1) % 2016
    s = cards([
        ("Blocs", h if h is not None else "-"),
        ("Difficulte", info.get("difficulty", "-")),
        ("Puissance du reseau", hs(nh)),
        ("Pairs", net.get("connections", "-")),
        ("Mempool (tx)", mp.get("size", "-")),
        ("Blocs avant ajustement", left),
        ("Solde disponible", bal.get("trusted", "-")),
        ("Solde immature", bal.get("immature", "-")),
        ("Mineurs ckpool", st.get("Workers", "-")),
        ("Hashrate 1 min (ckpool)", st.get("hashrate1m", "-")),
    ])
    s += sec("10 derniers blocs", blocks_table(blocks))
    if not info:
        s = "<p><b>Le noeud ne repond pas.</b></p>" + s
    return s


def view_mempool():
    info = rpc("getrawmempool", [True])
    mp = rpc("getmempoolinfo") or {}
    inf = rpc("getblockchaininfo") or {}
    txs = []
    for txid, t in (info or {}).items():
        vs = t.get("vsize") or 0
        fee = (t.get("fees") or {}).get("base", 0)
        rate = fee * 1e8 / vs if vs else 0
        txs.append((rate, txid, vs, fee, t.get("time")))
    txs.sort(reverse=True)
    edges = [(0, 2, "< 2"), (2, 5, "2 - 5"), (5, 10, "5 - 10"), (10, 20, "10 - 20"), (20, 50, "20 - 50"), (50, 1e18, "50 +")]
    hist = []
    for lo, hi, lab in edges:
        sel = [t for t in txs if lo <= t[0] < hi]
        hist.append((lab + " sat/vB", len(sel), sum(t[2] for t in sel)))
    s = cards([("Transactions en attente", mp.get("size", "-")), ("Taille", size(mp.get("bytes"))),
               ("Frais totaux", mp.get("total_fee", "-")), ("Frais minimum", mp.get("mempoolminfee", "-"))])
    if not txs:
        s += "<p class='muted'>Aucune transaction en attente. Elles sont confirmees tres vite tant que les blocs sont rapides.</p>"
    else:
        s += sec("Transactions (50 premieres, par frais decroissants)", table(["Txid", "Frais (sat/vB)", "Taille (vB)", "Attente"],
              [(t[1][:20] + "...", "%.1f" % t[0], t[2], ago(t[4])) for t in txs[:50]]))
        s += sec("Repartition des frais", table(["Tranche", "Transactions", "Taille (vB)"], hist))
    s += sec("Derniers blocs", blocks_table(recent_blocks(inf.get("blocks"))))
    return s


def view_ckpool(host):
    st = ckpool_status()
    s = sec("Connexion d'un mineur", kv([
        ("URL du pool", "stratum+tcp://%s:%s" % (host, STRATUM)),
        ("Utilisateur", "votre adresse rdt1... (ajoutez .nom pour nommer l'appareil)"),
        ("Mot de passe", "x"),
    ]))
    s += sec("Etat du pool", kv(scalars(st)) if st else "<p class='muted'>Aucune donnee de ckpool pour l'instant.</p>")
    users = ckpool_users()
    if users:
        s += "<h2>Utilisateurs et mineurs</h2>"
    for name, u in users:
        s += "<h3>%s</h3>%s" % (escape(name), kv(scalars(u)))
        ws = u.get("worker")
        for w in ws if isinstance(ws, list) else []:
            if isinstance(w, dict):
                s += "<div class='w'>%s</div>" % kv(scalars(w))
    return s


def page(host="IP-DE-L-UMBREL"):
    if VIEW == "node":
        body = view_node()
    elif VIEW == "mempool":
        body = view_mempool()
    elif VIEW == "ckpool":
        body = view_ckpool(host)
    else:
        body = view_dashboard()
    return ("<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<meta http-equiv='refresh' content='5'><title>" + TITLES.get(VIEW, "Radthal") + "</title>"
            "<style>" + CSS + "</style></head><body><h1>" + TITLES.get(VIEW, "Radthal") + "</h1>"
            "<small>Mise a jour automatique toutes les 5 secondes</small>" + body + "</body></html>")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        host = (self.headers.get("Host") or "IP-DE-L-UMBREL").split(":")[0]
        body = page(host).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
