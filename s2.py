"""S2 v1.0-R1 production code: Bovada opening-window price lag vs Pinnacle, scored by line-aware CLV.
Frozen spec: frozen/SPEC.md. Logs are append-only and SHA-256 hash-chained; all derived state is recomputed
from committed logs every run (idempotent). Never reads scores or outcomes."""
import os, json, math, hashlib, collections, datetime as dt, zoneinfo, importlib.util, requests

ROOT = os.path.dirname(os.path.abspath(__file__)); ET = zoneinfo.ZoneInfo("America/New_York")
P = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00")); F = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
S2_START = P("2026-10-20T00:00:00Z")
ENDPOINT = P("2028-03-01T00:00:00Z")          # no new T0 with K0 >= ENDPOINT
TRANSFORM_SHA = "806f9decf7825e202af33577f25478cb9586af269635ef37b12aa28b58427d4d"
TABLES_SHA = "eeff6a1a8625dc867c955ae9f53678a46d04a0021fe70ae3c89ee26f8edacc43"
EV_MIN = 0.02
OBS_INTERVAL = dt.timedelta(minutes=55)        # hourly observation runs (a run observes if no poll in the last 55 min)
FRESH_SIG = dt.timedelta(minutes=60); FRESH_CLOSE = dt.timedelta(minutes=30)
CLOSE_W = dt.timedelta(minutes=15); JIT = dt.timedelta(minutes=5)
WIN_MAX = dt.timedelta(hours=48); PRE_K = dt.timedelta(hours=24)
LOOKS = [100, 200, 300]
MECH = {"M1": ("NFL", "spreads"), "M2": ("NFL", "totals"), "M3": ("CFB", "totals")}
SPORT = {"NFL": "americanfootball_nfl", "CFB": "americanfootball_ncaaf"}
MARKETS = {"NFL": "spreads,totals", "CFB": "totals"}
TABLE = {"M1": "M1_NFL_spreads", "M2": "M2_NFL_totals", "M3": "M3_NCAAF_totals"}
STYPE = {"Over": "OVER", "Under": "UNDER", "home": "OVER", "away": "UNDER"}
SIDE_ORDER = {"Over": 0, "Under": 1, "home": 0, "away": 1}

def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()

def load_frozen():
    t = os.path.join(ROOT, "frozen", "s1_transform.py"); b = os.path.join(ROOT, "frozen", "s1_residual_tables.json")
    assert sha(t) == TRANSFORM_SHA, "transform hash mismatch - halt"
    assert sha(b) == TABLES_SHA, "residual table hash mismatch - halt"
    spec = importlib.util.spec_from_file_location("s1_transform", t); TR = importlib.util.module_from_spec(spec); spec.loader.exec_module(TR)
    return TR, json.load(open(b))

# ---------------- hash-chained append-only logs ----------------
def chain_verify(lines):
    prev = "GENESIS"
    for i, l in enumerate(lines):
        if json.loads(l)["prev_hash"] != prev: return False, i
        prev = hashlib.sha256(l.encode()).hexdigest()
    return True, len(lines)

def read_log(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p): return []
    lines = [l for l in open(p).read().split("\n") if l]
    ok, i = chain_verify(lines); assert ok, f"{rel} hash chain broken at line {i} - halt"
    return [json.loads(l) for l in lines]

def append(rel, rec, now):
    p = os.path.join(ROOT, rel); prev = "GENESIS"
    if os.path.exists(p):
        lines = [l for l in open(p).read().split("\n") if l]
        if lines: prev = hashlib.sha256(lines[-1].encode()).hexdigest()
    rec = dict(rec, prev_hash=prev, logged_at=F(now))
    with open(p, "a") as f: f.write(json.dumps(rec, sort_keys=True) + "\n")

# ---------------- quotes ----------------
def dec(a):
    a = float(a); return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)

def parse_quote(ev, book, mkt):
    """Return dict(sides={side:(point,decimal,american)}, last_update) or None if market absent/two-sided invalid."""
    for b in ev.get("bookmakers", []):
        if b["key"] != book: continue
        for m in b["markets"]:
            if m["key"] != mkt: continue
            o = m["outcomes"]; lu = m.get("last_update", b.get("last_update"))
            if mkt == "totals":
                d = {x["name"]: x for x in o}
                if set(d) != {"Over", "Under"} or d["Over"].get("point") is None or d["Over"]["point"] != d["Under"].get("point"): return None
                return dict(sides={s: (float(d[s]["point"]), dec(d[s]["price"]), d[s]["price"]) for s in ("Over", "Under")}, last_update=lu)
            d = {x["name"]: x for x in o}
            if ev["home_team"] not in d or ev["away_team"] not in d: return None
            h, a = d[ev["home_team"]], d[ev["away_team"]]
            if h.get("point") is None or a.get("point") is None or float(h["point"]) != -float(a["point"]): return None
            return dict(sides={"home": (float(h["point"]), dec(h["price"]), h["price"]), "away": (float(a["point"]), dec(a["price"]), a["price"])}, last_update=lu)
    return None

def fresh(q, t, lim):
    if not q or not q.get("last_update"): return False
    lu = P(q["last_update"]); return t - lim <= lu <= t

def novig(q, side):
    inv = {s: 1 / v[1] for s, v in q["sides"].items()}; return inv[side] / sum(inv.values())

# ---------------- observation run ----------------
def observe(now, io, ref_ok):
    """One observation run: /events + odds per sport; one hash-chained poll record."""
    polls = read_log("log/polls.jsonl"); seq = len(polls)
    rec = dict(seq=seq, request_time=F(now), sports={})
    for lg in ("NFL", "CFB"):
        r = dict(ok=False, error=None, receipt_time=None, events={})
        try:
            events = io["events"](SPORT[lg]); odds, rt = io["odds"](SPORT[lg], MARKETS[lg], "bovada,pinnacle")
            om = {e["id"]: e for e in odds}; r.update(ok=True, receipt_time=F(rt))
            for e in events:
                if not ref_ok(lg, e): continue                      # unmatched events are not observed
                oe = om.get(e["id"], dict(id=e["id"], home_team=e["home_team"], away_team=e["away_team"], bookmakers=[]))
                q = {}
                for mech, (mlg, mkt) in MECH.items():
                    if mlg != lg: continue
                    q[mech] = dict(bovada=parse_quote(oe, "bovada", mkt), pinnacle=parse_quote(oe, "pinnacle", mkt))
                r["events"][e["id"]] = dict(commence=e["commence_time"], quotes=q)
        except Exception as ex:
            r["error"] = type(ex).__name__
        rec["sports"][lg] = r
    append("log/polls.jsonl", rec, now)

def obs_stream(polls):
    """Yield (mech, event_id, poll_key, receipt_time, commence, bov, pin) for successful observations in (receipt, seq) order."""
    out = []
    for p in polls:
        for lg, r in p["sports"].items():
            if not r["ok"]: continue
            rt = P(r["receipt_time"])
            if rt < S2_START: continue
            for eid, e in r["events"].items():
                for mech, q in e["quotes"].items():
                    out.append((mech, eid, (rt, p["seq"]), rt, e["commence"], q["bovada"], q["pinnacle"]))
    out.sort(key=lambda x: x[2]); return out

def valid_sig(q, t): return q is not None and fresh(q, t, FRESH_SIG)

def present(q): return q is not None      # two-sided quote posted (opener detection: PREEXISTING / T0); freshness not applied

def mech_stopped(mech): return os.path.exists(os.path.join(ROOT, "looks", f"{mech}_STOPPED"))

def process(now):
    """Recompute T0/labels/wagers from the poll log and append any new records (idempotent)."""
    polls = read_log("log/polls.jsonl")
    t0s = {(r["event_id"], r["mech"]): r for r in read_log("log/t0.jsonl")}
    labels = {(r["event_id"], r["mech"]): r for r in read_log("log/labels.jsonl")}
    wagers = {(r["event_id"], r["mech"]): r for r in read_log("log/wagers.jsonl")}
    hist = collections.defaultdict(list)
    for o in obs_stream(polls): hist[(o[1], o[0])].append(o)
    for gm, obs in sorted(hist.items()):
        eid, mech = gm
        if gm in labels: continue                                   # terminal already
        first = obs[0]
        if present(first[5]):                                       # PREEXISTING (precedence 1)
            append("log/labels.jsonl", dict(event_id=eid, mech=mech, label="PREEXISTING", at_seq=first[2][1]), now); continue
        t0 = t0s.get(gm)
        if t0 is None:
            cand = next((o for o in obs[1:] if present(o[5])), None)
            if cand is None:
                last_commence = P(obs[-1][4])
                if now >= last_commence - PRE_K:                    # NO-BOVADA-OPEN (precedence 2)
                    append("log/labels.jsonl", dict(event_id=eid, mech=mech, label="NO-BOVADA-OPEN"), now)
                continue
            K0 = P(cand[4])
            if K0 >= ENDPOINT: continue                             # no new T0 at/after endpoint (out of S2 period)
            if mech_stopped(mech): continue                         # no new T0 after a terminal statistical decision
            t0 = dict(event_id=eid, mech=mech, T0=F(cand[3]), T0_seq=cand[2][1], K0=F(K0))
            append("log/t0.jsonl", t0, now); t0s[gm] = t0
        T0, K0 = P(t0["T0"]), P(t0["K0"]); wend = min(T0 + WIN_MAX, K0 - PRE_K)
        if T0 > K0 - PRE_K:                                         # LATE-OPEN (precedence 3)
            append("log/labels.jsonl", dict(event_id=eid, mech=mech, label="LATE-OPEN"), now); continue
        inwin = [o for o in obs if T0 <= o[3] <= wend and (o[3], o[2][1]) >= (T0, t0["T0_seq"])]
        sig, saw_pin, saw_same = None, False, False
        for o in inwin:
            bov, pin, t = o[5], o[6], o[3]
            if not valid_sig(bov, t): continue
            if not valid_sig(pin, t): continue
            saw_pin = True
            c = []
            for side, (pt, d, am) in bov["sides"].items():
                if pin["sides"][side][0] != pt: continue
                c.append((novig(pin, side) * d - 1, side, pt, d, am, novig(pin, side)))
            if not c: continue
            saw_same = True
            ev = sorted(c, key=lambda x: (-x[0], SIDE_ORDER[x[1]]))[0]
            if ev[0] >= EV_MIN: sig = (o, ev); break
        if sig and gm not in wagers and not mech_stopped(mech):    # SIGNAL (precedence 4)
            o, (evv, side, pt, d, am, pf) = sig
            w = dict(event_id=eid, mech=mech, side=side, line=pt, price=am, dec=d, EV=evv, p_fair=pf, signal_seq=o[2][1],
                     signal_time=F(o[3]), T0=t0["T0"], K0=t0["K0"])
            append("log/wagers.jsonl", w, now); wagers[gm] = w
            append("log/labels.jsonl", dict(event_id=eid, mech=mech, label="SIGNAL"), now); continue
        if now > wend:                                              # window closed without signal
            if mech_stopped(mech) and gm not in wagers:
                lab = "MECHANISM-STOPPED"                           # reported for audit (see notes)
            else:
                lab = "NO-PINNACLE-IN-WINDOW" if not saw_pin else "NO-SAME-LINE-IN-WINDOW" if not saw_same else "EV-BELOW-2"
            append("log/labels.jsonl", dict(event_id=eid, mech=mech, label=lab), now)

# ---------------- close ----------------
def close_capture(now, io):
    done = {(r["event_id"], r["mech"]) for r in read_log("log/clv.jsonl")}
    open_w = [w for w in read_log("log/wagers.jsonl") if (w["event_id"], w["mech"]) not in done and P(w["K0"]) - CLOSE_W <= now <= P(w["K0"])]
    calls = {}
    for w in sorted(open_w, key=lambda w: (w["K0"], w["event_id"], w["mech"])):
        lg, mkt = MECH[w["mech"]]
        if (lg, mkt) not in calls:
            resp, err, rt = None, None, None
            for _ in range(3):
                try: resp, rt = io["odds"](SPORT[lg], mkt, "pinnacle"); break
                except Exception as ex: err = type(ex).__name__
            calls[(lg, mkt)] = (resp, err, rt)
        resp, err, rt = calls[(lg, mkt)]
        ev = next((e for e in (resp or []) if e["id"] == w["event_id"]), None)
        append("log/close_caps.jsonl", dict(event_id=w["event_id"], mech=w["mech"], ok=resp is not None, error=err,
               capture_time=F(rt or now), listed=ev is not None, commence=ev["commence_time"] if ev else None,
               quote=parse_quote(ev, "pinnacle", mkt) if ev else None), now)

def finalize_close(now, TR, tables):
    done = {(r["event_id"], r["mech"]) for r in read_log("log/clv.jsonl")}; caps = read_log("log/close_caps.jsonl")
    for w in sorted(read_log("log/wagers.jsonl"), key=lambda w: (w["K0"], w["event_id"], w["mech"])):
        gm = (w["event_id"], w["mech"]); K0 = P(w["K0"])
        if gm in done or now <= K0: continue
        mine = sorted([c for c in caps if (c["event_id"], c["mech"]) == gm and K0 - CLOSE_W <= P(c["capture_time"]) <= K0 and c["ok"]],
                      key=lambda c: c["capture_time"], reverse=True)
        status, rec, listing_problem = None, None, False
        for c in mine:
            if not c["listed"] or abs(P(c["commence"]) - K0) > JIT: listing_problem = True; continue
            q = c["quote"]
            if not fresh(q, P(c["capture_time"]), FRESH_CLOSE): continue
            side = w["side"]; Lc_pt, dc, _ = q["sides"][side]
            L = lambda pt: pt if MECH[w["mech"]][1] == "totals" else (-pt if side == "home" else pt)
            Lb, Lc = L(w["line"]), L(Lc_pt); p_c = novig(q, side)
            res = TR.transform(tables[TABLE[w["mech"]]], STYPE[side], Lb, w["dec"], Lc, p_c)
            base = dict(event_id=w["event_id"], mech=w["mech"], K0=w["K0"], side=side, L_b=Lb, d=w["dec"], L_c=Lc, p_c=p_c, capture_time=c["capture_time"])
            rec = dict(base, status="INVALID-TRANSFORM") if res is None else dict(base, status="EVALUABLE", W=res["W"], P=res["P"], L=res["L"], clipped=res["clipped"], CLV=res["CLV"])
            break
        if rec is None:
            rec = dict(event_id=w["event_id"], mech=w["mech"], K0=w["K0"], status="X4-CLOSE" if listing_problem else "ERROR-NO-CLOSE")
        append("log/clv.jsonl", rec, now)

# ---------------- looks ----------------
def z_of(v, cl):
    n = len(v); mu = sum(v) / n; g = collections.defaultdict(float)
    for x, c in zip(v, cl): g[c] += x - mu
    G = len(g)
    if G < 2: return mu, None, None, G
    se = math.sqrt(G / (G - 1) * sum(s * s for s in g.values())) / n
    if se == 0: return mu, 0.0, (math.inf if mu > 0 else -math.inf if mu < 0 else 0.0), G
    return mu, se, mu / se, G

def looks(now):
    clv = read_log("log/clv.jsonl"); term = {(r["event_id"], r["mech"]) for r in clv}; wagers = read_log("log/wagers.jsonl")
    for mech in MECH:
        if mech_stopped(mech): continue
        ev = sorted([r for r in clv if r["mech"] == mech and r["status"] == "EVALUABLE"], key=lambda r: (r["K0"], r["event_id"]))
        for N in LOOKS:
            fn = os.path.join(ROOT, "looks", f"{mech}_{N}.json")
            if os.path.exists(fn): continue
            if len(ev) < N: break
            KN = ev[N - 1]["K0"]
            if any(w["mech"] == mech and w["K0"] <= KN and (w["event_id"], mech) not in term for w in wagers): break   # watermark
            mem = ev[:N]; mu, se, z, G = z_of([r["CLV"] for r in mem], [P(r["K0"]).astimezone(ET).date().isoformat() for r in mem])
            d = "CONTINUE"
            if z is not None and z >= 2.24: d = "CLV GATE PASSED"
            elif N == 100 and z is not None and z <= -1.28: d = "KILL"
            elif N in (200, 300) and mu <= 0: d = "KILL"
            elif N == 300: d = "KILL"
            labs = collections.Counter(r["label"] for r in read_log("log/labels.jsonl") if r["mech"] == mech)
            json.dump(dict(mechanism=mech, look=N, members=[r["event_id"] for r in mem], K0_of_Nth=KN, mean_clv=mu, se_cr1=se,
                           z=(str(z) if z in (math.inf, -math.inf) else z), clusters=G, clipped=sum(bool(r["clipped"]) for r in mem),
                           label_counts=dict(labs), decision=d, computed_at=F(now)), open(fn, "w"), indent=1, sort_keys=True)
            if d != "CONTINUE":
                open(os.path.join(ROOT, "looks", f"{mech}_STOPPED"), "w").write(f"{d} at look {N}. Freeze; independent audit. No betting authorized.\n")
                break

# ---------------- run ----------------
def run(now, io, ref_ok):
    TR, tables = load_frozen()
    polls = read_log("log/polls.jsonl")
    last = max((P(p["request_time"]) for p in polls), default=None)
    if now >= S2_START and (last is None or now - last >= OBS_INTERVAL): observe(now, io, ref_ok)
    process(now)
    close_capture(now, io)
    finalize_close(now, TR, tables)
    looks(now)

# ---------------- live I/O and reference matching ----------------
def live_io():
    key = os.environ["ODDS_API_KEY"]; B = "https://api.the-odds-api.com/v4/sports"
    def events(sport):
        r = requests.get(f"{B}/{sport}/events", params=dict(apiKey=key), timeout=60); r.raise_for_status(); return r.json()
    def odds(sport, markets, books):
        r = requests.get(f"{B}/{sport}/odds", params=dict(apiKey=key, markets=markets, bookmakers=books, oddsFormat="american"), timeout=60)
        r.raise_for_status(); return r.json(), dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return dict(events=events, odds=odds)

def live_ref():
    import pandas as pd
    names = json.load(open(os.path.join(ROOT, "frozen", "names.json"))); now = dt.datetime.now(dt.timezone.utc)
    season = now.year if now.month >= 3 else now.year - 1; cache = os.path.join(ROOT, "state", f"ref_{season}.json")
    if os.path.exists(cache) and json.load(open(cache)).get("day") == now.date().isoformat(): R = json.load(open(cache))
    else:
        nfl = pd.read_csv("https://github.com/nflverse/nfldata/raw/master/data/games.csv", usecols=["season", "gameday", "home_team", "away_team"])
        nfl = nfl[nfl.season == season]
        cfb = pd.read_csv(f"https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/schedules/csv/cfb_schedules_{season}.csv",
                          usecols=["start_date", "home_team", "away_team", "home_division", "away_division"])
        cfb = cfb[(cfb.home_division == "fbs") | (cfb.away_division == "fbs")]
        R = dict(day=now.date().isoformat(),
                 nfl=[[names["nfl_abbr"][r.home_team], names["nfl_abbr"][r.away_team], r.gameday] for r in nfl.itertuples()],
                 cfb=[[r.home_team, r.away_team, r.start_date[:10]] for r in cfb.itertuples()],
                 cfb_names=sorted(set(cfb.home_team) | set(cfb.away_team), key=len, reverse=True))
        json.dump(R, open(cache, "w"))
    def cname(o):
        if o in names["cfb_alias"]: return names["cfb_alias"][o]
        for n in R["cfb_names"]:
            if o == n or o.startswith(n + " "): return n
        return None
    def ok(lg, e):
        c = P(e["commence_time"])
        if lg == "NFL": pair, rows = frozenset((e["home_team"], e["away_team"])), R["nfl"]
        else: pair, rows = frozenset((cname(e["home_team"]), cname(e["away_team"]))), R["cfb"]
        return any(frozenset((h, a)) == pair and abs((dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc) - c).days) <= 4 for h, a, d in rows)
    return ok

if __name__ == "__main__":
    run(dt.datetime.now(dt.timezone.utc).replace(microsecond=0), live_io(), live_ref())
