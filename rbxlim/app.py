import os, sys, json, sqlite3, hashlib, secrets, time, threading
from datetime import datetime
from flask import Flask, request, jsonify, session, send_from_directory
from flask_socketio import SocketIO, emit, join_room
import requests as http

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = Flask(__name__, static_folder="static")

BASE_DIR    = os.path.dirname(__file__)
DB_PATH     = os.path.join(BASE_DIR, "rbxlim.db")
CONFIG_PATH = os.path.join(BASE_DIR, "..", "config.json")
TRADE_BOT   = "http://localhost:5000"

def _get_secret_key():
    _cfg_path = CONFIG_PATH
    cfg = {}
    if os.path.exists(_cfg_path):
        with open(_cfg_path) as f:
            cfg = json.load(f)
    key = cfg.get("flask_secret")
    if not key:
        key = secrets.token_hex(32)
        cfg["flask_secret"] = key
        with open(_cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
    return key

app.secret_key = _get_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

RAP_PER_USD = 1000 / 3   # 1 000 RAP = $3  →  333.33 RAP / $1


# ── Config ───────────────────────────────────────────────────

def load_cfg():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def save_cfg(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Database ─────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            roblox_id   TEXT UNIQUE NOT NULL,
            roblox_name TEXT NOT NULL,
            balance     REAL DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS verification_codes (
            roblox_name TEXT PRIMARY KEY,
            code        TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS ltc_addresses (
            user_id            INTEGER PRIMARY KEY,
            address            TEXT UNIQUE NOT NULL,
            derivation_index   INTEGER UNIQUE NOT NULL,
            monitored_balance  REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ltc_counter (
            id      INTEGER PRIMARY KEY,
            counter INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              INTEGER NOT NULL,
            item_name            TEXT NOT NULL,
            target_id            TEXT NOT NULL,
            collectible_item_id  TEXT,
            instance_id          TEXT,
            rap                  INTEGER DEFAULT 0,
            thumbnail_url        TEXT,
            acquired_at          TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS coinflips (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id      INTEGER NOT NULL,
            creator_item_id INTEGER NOT NULL,
            server_seed     TEXT NOT NULL,
            server_seed_hash TEXT NOT NULL,
            client_seed     TEXT DEFAULT '',
            status          TEXT DEFAULT 'open',
            winner_side     TEXT,
            joiner_id       INTEGER,
            joiner_item_id  INTEGER,
            is_bot_game     INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            message    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS pending_deposits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            trade_id    TEXT,
            bot_id      TEXT NOT NULL DEFAULT '1',
            status      TEXT DEFAULT 'pending',
            item_name   TEXT,
            target_id   TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS withdraw_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            item_name   TEXT,
            target_id   TEXT,
            rap         INTEGER DEFAULT 0,
            bot_id      TEXT DEFAULT '1',
            status      TEXT DEFAULT 'sent',
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.execute("INSERT OR IGNORE INTO ltc_counter VALUES (1, 0)")
    # ── Migrate existing tables: add columns if missing ──────────
    for col, col_def in [("item_name", "TEXT"), ("target_id", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE pending_deposits ADD COLUMN {col} {col_def}")
            conn.commit()
        except Exception:
            pass
    # ── Reset any fake LTC addresses (LTC_DEV / LTC_FALLBACK / LTC_ERR) ──
    bad = conn.execute(
        "SELECT COUNT(*) FROM ltc_addresses WHERE address NOT LIKE 'ltc1%'"
    ).fetchone()[0]
    if bad:
        print(f"[init] Clearing {bad} invalid LTC addresses — will regenerate on next deposit request")
        conn.execute("DELETE FROM ltc_addresses WHERE address NOT LIKE 'ltc1%'")
        conn.execute("UPDATE ltc_counter SET counter = (SELECT COALESCE(MAX(derivation_index)+1,0) FROM ltc_addresses) WHERE id=1")
    conn.commit()
    conn.close()


# ── LTC helpers ──────────────────────────────────────────────

def ensure_ltc_mnemonic():
    cfg = load_cfg()
    phrase = cfg.get("ltc_mnemonic", "")
    # regenerate if missing or looks like a hex string (old bad format)
    if not phrase or (len(phrase) == 64 and " " not in phrase):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from bitcoinlib.mnemonic import Mnemonic as BM
            phrase = BM().generate()
        cfg["ltc_mnemonic"] = phrase
        save_cfg(cfg)
    return phrase

def generate_ltc_address(index: int) -> str:
    """BIP44 LTC address — uses pbkdf2 directly, no mnemonic word validation."""
    mnemonic = ensure_ltc_mnemonic()
    try:
        import warnings
        # Derive seed the BIP39 way (pbkdf2) — bypasses bitcoinlib word list check
        seed = hashlib.pbkdf2_hmac(
            "sha512",
            mnemonic.encode("utf-8"),
            b"mnemonic",   # standard BIP39 salt (no passphrase)
            2048
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from bitcoinlib.keys import HDKey
            root  = HDKey.from_seed(seed, network="litecoin")
            child = root.subkey_for_path(f"m/44'/2'/0'/0/{index}")
            addr  = child.address()
        print(f"[LTC] index={index} addr={addr}")
        return addr
    except Exception as e:
        import traceback
        print(f"[LTC addr gen FAILED]\n{traceback.format_exc()}")
        h = hashlib.sha256(f"{mnemonic}:{index}".encode()).hexdigest()
        return "LTC_ERR_" + h[:28]

def get_ltc_price() -> float:
    try:
        r = http.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd",
            timeout=5
        )
        return r.json()["litecoin"]["usd"]
    except Exception:
        return 80.0

def check_ltc_received(address: str) -> float:
    try:
        r    = http.get(
            f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance",
            timeout=10
        )
        data = r.json()
        confirmed   = data.get("total_received", 0) / 1e8
        unconfirmed = max(0, data.get("unconfirmed_balance", 0)) / 1e8
        return confirmed + unconfirmed
    except Exception:
        return 0.0

def monitor_ltc():
    while True:
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT user_id, address, monitored_balance FROM ltc_addresses"
            ).fetchall()
            price = get_ltc_price()
            for row in rows:
                total = check_ltc_received(row["address"])
                if total > row["monitored_balance"] + 1e-9:
                    new_ltc = total - row["monitored_balance"]
                    usd     = round(new_ltc * price, 4)
                    conn.execute(
                        "UPDATE ltc_addresses SET monitored_balance=? WHERE user_id=?",
                        (total, row["user_id"])
                    )
                    conn.execute(
                        "UPDATE users SET balance=balance+? WHERE id=?",
                        (usd, row["user_id"])
                    )
                    conn.commit()
                    user = conn.execute(
                        "SELECT balance FROM users WHERE id=?", (row["user_id"],)
                    ).fetchone()
                    socketio.emit("balance_update", {
                        "balance": round(user["balance"], 4),
                        "added":   usd,
                    }, room=f"u{row['user_id']}")
                    socketio.emit("toast", {
                        "type":    "success",
                        "message": f"Deposited ${usd:.2f} ({new_ltc:.6f} LTC)",
                    }, room=f"u{row['user_id']}")
            conn.close()
        except Exception as e:
            print(f"[LTC monitor] {e}")
        time.sleep(15)


# ── Roblox API ───────────────────────────────────────────────

def rbx_user_by_name(name: str):
    try:
        r = http.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [name], "excludeBannedUsers": False},
            timeout=8
        )
        data = r.json().get("data", [])
        return data[0] if data else None
    except Exception:
        return None

def rbx_description(uid) -> str:
    try:
        return http.get(f"https://users.roblox.com/v1/users/{uid}", timeout=6).json().get("description", "")
    except Exception:
        return ""

def rbx_headshot(uid) -> str:
    try:
        r = http.get(
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={uid}&size=150x150&format=Png",
            timeout=6
        )
        d = r.json().get("data", [])
        return d[0].get("imageUrl", "") if d else ""
    except Exception:
        return ""

def rbx_item_thumb(target_id) -> str:
    try:
        r = http.get(
            f"https://thumbnails.roblox.com/v1/assets"
            f"?assetIds={target_id}&size=150x150&format=Png",
            timeout=6
        )
        d = r.json().get("data", [])
        return d[0].get("imageUrl", "") if d else ""
    except Exception:
        return ""


# ── Auth helpers ─────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return dict(u) if u else None


# ── Roblox direct API (uses bot cookie) ──────────────────────

def roblox_cookie(bot_id="1") -> str:
    cfg = load_cfg()
    return cfg.get("bots", {}).get(bot_id, {}).get("roblosecurity", "")

def rbx_get_tradable(user_id, bot_id="1") -> list:
    """Fetch all tradable items (including on-hold) for any user using bot's cookie."""
    cookie = roblox_cookie(bot_id)
    url    = (f"https://trades.roblox.com/v2/users/{user_id}/tradableitems"
              f"?sortBy=CreationTime&cursor=&limit=50&sortOrder=Desc")
    r = http.get(url, headers={"Cookie": f".ROBLOSECURITY={cookie}"}, timeout=12)
    r.raise_for_status()
    return r.json().get("items", [])

def rbx_get_hold_map(user_id, bot_id="1") -> dict:
    """Returns {instance_id: is_on_hold} for all of a user's tradable items."""
    items = rbx_get_tradable(user_id, bot_id)
    result = {}
    for itm in items:
        for inst in itm.get("instances", []):
            result[inst["collectibleItemInstanceId"]] = inst.get("isOnHold", False)
    return result

def rbx_get_uid(bot_id="1") -> str:
    """Get Roblox user ID for a bot via the authenticated endpoint."""
    cookie = roblox_cookie(bot_id)
    r = http.get(
        "https://users.roblox.com/v1/users/authenticated",
        headers={"Cookie": f".ROBLOSECURITY={cookie}"},
        timeout=8
    )
    return str(r.json().get("id", ""))


# ── Bot inventory ────────────────────────────────────────────

_market_cache      = []
_market_cache_time = 0

def bot_inventory(force=False):
    global _market_cache, _market_cache_time
    if not force and time.time() - _market_cache_time < 90:
        return _market_cache
    items = []
    cfg   = load_cfg()
    for bot_id in cfg.get("bots", {}):
        try:
            uid  = rbx_get_uid(bot_id)
            if not uid:
                print(f"[market] bot={bot_id} could not get uid")
                continue
            raw  = rbx_get_tradable(uid, bot_id)
            for itm in raw:
                rap = itm.get("recentAveragePrice") or 0
                for inst in itm.get("instances", []):
                    # Skip every on-hold instance — bot side strict filter
                    if inst.get("isOnHold", False):
                        continue
                    tid = str(itm["itemTarget"]["targetId"])
                    items.append({
                        "bot_id":              bot_id,
                        "item_name":           itm["itemName"],
                        "target_id":           tid,
                        "collectible_item_id": itm["collectibleItemId"],
                        "instance_id":         inst["collectibleItemInstanceId"],
                        "rap":                 rap,
                        "price_usd":           round(rap / RAP_PER_USD, 2),
                        "thumbnail_url":       rbx_item_thumb(tid),
                    })
            print(f"[market] bot={bot_id} uid={uid} items={len(raw)}")
        except Exception as e:
            print(f"[market] bot={bot_id} error: {e}")
    _market_cache      = items
    _market_cache_time = time.time()
    return items


# ── Routes ───────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# Auth

@app.route("/api/auth/request-code", methods=["POST"])
def request_code():
    name = (request.json or {}).get("username", "").strip()
    if not name:
        return jsonify({"error": "Username required"}), 400
    u = rbx_user_by_name(name)
    if not u:
        return jsonify({"error": "Roblox user not found"}), 404
    code = f"RBXLIM-{secrets.token_hex(4).upper()}"
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO verification_codes (roblox_name, code) VALUES (?,?)",
        (name.lower(), code)
    )
    conn.commit()
    conn.close()
    return jsonify({"code": code, "roblox_id": u["id"], "roblox_name": u["name"]})

@app.route("/api/auth/verify", methods=["POST"])
def verify():
    name = (request.json or {}).get("username", "").strip()
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM verification_codes WHERE roblox_name=?", (name.lower(),)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "No code found — request one first"}), 400
    u = rbx_user_by_name(name)
    if not u:
        conn.close()
        return jsonify({"error": "Roblox user not found"}), 404
    desc = rbx_description(u["id"])
    if row["code"] not in desc:
        conn.close()
        return jsonify({"error": "Code not found in your Roblox description yet"}), 400
    existing = conn.execute(
        "SELECT * FROM users WHERE roblox_id=?", (str(u["id"]),)
    ).fetchone()
    if existing:
        user_id = existing["id"]
    else:
        cur     = conn.execute(
            "INSERT INTO users (roblox_id, roblox_name) VALUES (?,?)",
            (str(u["id"]), u["name"])
        )
        user_id = cur.lastrowid
    conn.execute("DELETE FROM verification_codes WHERE roblox_name=?", (name.lower(),))
    conn.commit()
    conn.close()
    session["user_id"] = user_id
    return jsonify({
        "success":     True,
        "id":          user_id,
        "roblox_name": u["name"],
        "roblox_id":   str(u["id"]),
        "thumbnail":   rbx_headshot(u["id"]),
        "balance":     0,
    })

@app.route("/api/auth/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    return jsonify({**u, "thumbnail": rbx_headshot(u["roblox_id"])})

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})

# Marketplace

@app.route("/api/marketplace")
def marketplace():
    return jsonify({"items": bot_inventory()})

@app.route("/api/marketplace/buy-bulk", methods=["POST"])
def buy_items_bulk():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    tids  = (request.json or {}).get("target_ids", [])
    if not tids:
        return jsonify({"error": "No items specified"}), 400
    items = bot_inventory()
    conn  = get_db()
    urow  = conn.execute("SELECT balance FROM users WHERE id=?", (u["id"],)).fetchone()
    bal   = urow["balance"]
    bought, errors = [], []
    for tid in tids:
        item = next((i for i in items if i["target_id"] == tid), None)
        if not item:
            errors.append({"target_id": tid, "error": "Not in marketplace"})
            continue
        if bal < item["price_usd"]:
            errors.append({"target_id": tid, "error": f"Insufficient balance (need ${item['price_usd']:.2f})"})
            continue
        bal -= item["price_usd"]
        conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (item["price_usd"], u["id"]))
        conn.execute(
            "INSERT INTO inventory (user_id,item_name,target_id,collectible_item_id,instance_id,rap,thumbnail_url) VALUES (?,?,?,?,?,?,?)",
            (u["id"], item["item_name"], tid, item["collectible_item_id"], item["instance_id"], item["rap"], item["thumbnail_url"])
        )
        bought.append(item["item_name"])
    conn.commit()
    new_bal = conn.execute("SELECT balance FROM users WHERE id=?", (u["id"],)).fetchone()["balance"]
    conn.close()
    global _market_cache_time
    _market_cache_time = 0
    return jsonify({"success": True, "bought": bought, "errors": errors, "balance": round(new_bal, 4)})


@app.route("/api/marketplace/buy", methods=["POST"])
def buy_item():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    tid   = (request.json or {}).get("target_id", "")
    items = bot_inventory()
    item  = next((i for i in items if i["target_id"] == tid), None)
    if not item:
        return jsonify({"error": "Item not in marketplace"}), 404
    conn  = get_db()
    urow  = conn.execute("SELECT balance FROM users WHERE id=?", (u["id"],)).fetchone()
    if urow["balance"] < item["price_usd"]:
        conn.close()
        return jsonify({"error": f"Need ${item['price_usd']:.2f}, have ${urow['balance']:.2f}"}), 400
    conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (item["price_usd"], u["id"]))
    conn.execute(
        "INSERT INTO inventory (user_id,item_name,target_id,collectible_item_id,instance_id,rap,thumbnail_url) VALUES (?,?,?,?,?,?,?)",
        (u["id"], item["item_name"], tid,
         item["collectible_item_id"], item["instance_id"],
         item["rap"], item["thumbnail_url"])
    )
    conn.commit()
    new_bal = conn.execute("SELECT balance FROM users WHERE id=?", (u["id"],)).fetchone()["balance"]
    conn.close()
    global _market_cache_time
    _market_cache_time = 0
    return jsonify({"success": True, "balance": round(new_bal, 4)})

# Deposit LTC

@app.route("/api/deposit/ltc", methods=["POST"])
def deposit_ltc():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    conn = get_db()
    row  = conn.execute(
        "SELECT address FROM ltc_addresses WHERE user_id=?", (u["id"],)
    ).fetchone()
    if row:
        conn.close()
        return jsonify({"address": row["address"], "ltc_price": get_ltc_price()})
    ctr = conn.execute("SELECT counter FROM ltc_counter WHERE id=1").fetchone()["counter"]
    conn.execute("UPDATE ltc_counter SET counter=counter+1 WHERE id=1")
    addr = generate_ltc_address(ctr)
    conn.execute(
        "INSERT INTO ltc_addresses (user_id,address,derivation_index) VALUES (?,?,?)",
        (u["id"], addr, ctr)
    )
    conn.commit()
    conn.close()
    return jsonify({"address": addr, "ltc_price": get_ltc_price()})

@app.route("/api/deposit/ltc/price")
def ltc_price():
    return jsonify({"price": get_ltc_price()})

# Inventory

@app.route("/api/inventory")
def inventory():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    conn  = get_db()
    items = [dict(i) for i in conn.execute(
        "SELECT * FROM inventory WHERE user_id=? ORDER BY acquired_at DESC", (u["id"],)
    ).fetchall()]
    conn.close()
    # Annotate hold status — check which instances are on hold in the bot's inventory
    try:
        cfg      = load_cfg()
        bot_id   = next(iter(cfg.get("bots", {})), "1")
        bot_uid  = rbx_get_uid(bot_id)
        hold_map = rbx_get_hold_map(bot_uid, bot_id)   # {instance_id: is_on_hold}
        for item in items:
            inst_id = item.get("instance_id")
            if inst_id and inst_id in hold_map:
                item["is_on_hold"] = hold_map[inst_id]
            else:
                item["is_on_hold"] = False
    except Exception as e:
        print(f"[inventory hold check] {e}")
        for item in items:
            item["is_on_hold"] = False
    return jsonify({"items": items})

# Coinflip

@app.route("/api/coinflip/list")
def list_flips():
    conn  = get_db()
    rows  = conn.execute("""
        SELECT cf.*, u.roblox_name as cname, u.roblox_id as crid,
               i.item_name, i.rap, i.thumbnail_url
        FROM coinflips cf
        JOIN users     u ON cf.creator_id      = u.id
        JOIN inventory i ON cf.creator_item_id = i.id
        WHERE cf.status='open'
        ORDER BY cf.created_at DESC
    """).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["creator_thumbnail"] = rbx_headshot(d["crid"])
        out.append(d)
    return jsonify({"flips": out})

@app.route("/api/coinflip/create", methods=["POST"])
def create_flip():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    item_id = (request.json or {}).get("item_id")
    conn    = get_db()
    item    = conn.execute(
        "SELECT * FROM inventory WHERE id=? AND user_id=?", (item_id, u["id"])
    ).fetchone()
    if not item:
        conn.close()
        return jsonify({"error": "Item not in your inventory"}), 404
    ss   = secrets.token_hex(32)
    ssh  = hashlib.sha256(ss.encode()).hexdigest()
    cur  = conn.execute(
        "INSERT INTO coinflips (creator_id,creator_item_id,server_seed,server_seed_hash) VALUES (?,?,?,?)",
        (u["id"], item_id, ss, ssh)
    )
    fid  = cur.lastrowid
    conn.commit()
    conn.close()
    flip_data = {
        "id":               fid,
        "creator_name":     u["roblox_name"],
        "creator_thumbnail": rbx_headshot(u["roblox_id"]),
        "item_name":        item["item_name"],
        "rap":              item["rap"],
        "thumbnail_url":    item["thumbnail_url"],
        "server_seed_hash": ssh,
        "status":           "open",
    }
    socketio.emit("flip_new", flip_data)
    return jsonify({"success": True, "flip": flip_data})

def _resolve_flip(conn, flip, joiner_id, joiner_item_id, client_seed, is_bot=False):
    combined    = f"{flip['server_seed']}:{client_seed}"
    h           = hashlib.sha256(combined.encode()).hexdigest()
    result_num  = int(h[:8], 16) % 100
    winner_side = "creator" if result_num < 50 else "joiner"
    winner_id   = flip["creator_id"] if winner_side == "creator" else joiner_id
    loser_id    = joiner_id if winner_side == "creator" else flip["creator_id"]
    conn.execute("UPDATE inventory SET user_id=? WHERE id=?", (winner_id, flip["creator_item_id"]))
    conn.execute("UPDATE inventory SET user_id=? WHERE id=?", (winner_id, joiner_item_id))
    conn.execute("""
        UPDATE coinflips SET status='completed', joiner_id=?, joiner_item_id=?,
               client_seed=?, winner_side=?, is_bot_game=? WHERE id=?
    """, (joiner_id, joiner_item_id, client_seed, winner_side, int(is_bot), flip["id"]))
    conn.commit()
    return winner_side, winner_id, h

@app.route("/api/coinflip/join", methods=["POST"])
def join_flip():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    data    = request.json or {}
    fid     = data.get("flip_id")
    iid     = data.get("item_id")
    cseed   = data.get("client_seed") or secrets.token_hex(8)
    conn    = get_db()
    flip    = conn.execute(
        "SELECT * FROM coinflips WHERE id=? AND status='open'", (fid,)
    ).fetchone()
    if not flip:
        conn.close()
        return jsonify({"error": "Flip not found or already taken"}), 404
    if flip["creator_id"] == u["id"]:
        conn.close()
        return jsonify({"error": "Cannot join your own flip"}), 400
    item = conn.execute(
        "SELECT * FROM inventory WHERE id=? AND user_id=?", (iid, u["id"])
    ).fetchone()
    if not item:
        conn.close()
        return jsonify({"error": "Item not in your inventory"}), 404
    ws, wid, h = _resolve_flip(conn, flip, u["id"], iid, cseed)
    conn.close()
    result = {
        "flip_id":     fid,
        "winner_side": ws,
        "winner_id":   wid,
        "server_seed": flip["server_seed"],
        "client_seed": cseed,
        "hash":        h,
    }
    socketio.emit("flip_result", result)
    return jsonify({"success": True, **result})

@app.route("/api/coinflip/bot-join", methods=["POST"])
def bot_join():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    fid  = (request.json or {}).get("flip_id")
    conn = get_db()
    flip = conn.execute(
        "SELECT * FROM coinflips WHERE id=? AND status='open'", (fid,)
    ).fetchone()
    if not flip:
        conn.close()
        return jsonify({"error": "Flip not found"}), 404
    if flip["creator_id"] != u["id"]:
        conn.close()
        return jsonify({"error": "Only the creator can call the bot"}), 403
    creator_item = conn.execute(
        "SELECT * FROM inventory WHERE id=?", (flip["creator_item_id"],)
    ).fetchone()
    creator_rap  = creator_item["rap"] or 0
    bot_items    = bot_inventory()
    avail        = [i for i in bot_items if not i.get("is_on_hold")]
    if not avail:
        conn.close()
        return jsonify({"error": "No bot items available"}), 404
    # pick closest RAP
    pick = min(avail, key=lambda i: abs((i["rap"] or 0) - creator_rap))
    # get/create bot user
    bot_u = conn.execute("SELECT id FROM users WHERE roblox_id='__BOT__'").fetchone()
    if not bot_u:
        cur   = conn.execute(
            "INSERT INTO users (roblox_id, roblox_name, balance) VALUES ('__BOT__','Bot',999999)"
        )
        bot_uid = cur.lastrowid
    else:
        bot_uid = bot_u["id"]
    cur2 = conn.execute(
        "INSERT INTO inventory (user_id,item_name,target_id,collectible_item_id,instance_id,rap,thumbnail_url) VALUES (?,?,?,?,?,?,?)",
        (bot_uid, pick["item_name"], pick["target_id"],
         pick["collectible_item_id"], pick["instance_id"],
         pick["rap"], pick["thumbnail_url"])
    )
    bot_iid = cur2.lastrowid
    conn.commit()
    cseed = secrets.token_hex(8)
    ws, wid, h = _resolve_flip(conn, flip, bot_uid, bot_iid, cseed, is_bot=True)
    conn.close()
    # if player wins, remove bot item from marketplace cache
    if wid == u["id"]:
        global _market_cache
        _market_cache = [i for i in _market_cache if i["instance_id"] != pick["instance_id"]]
    else:
        # bot wins → refresh cache so player's item shows in marketplace
        global _market_cache_time
        _market_cache_time = 0
    result = {
        "flip_id":     fid,
        "winner_side": ws,
        "winner_id":   wid,
        "bot_won":     wid == bot_uid,
        "server_seed": flip["server_seed"],
        "client_seed": cseed,
        "hash":        h,
    }
    socketio.emit("flip_result", result)
    return jsonify({"success": True, **result})

# Withdraw / Deposit items

@app.route("/api/withdraw/user-items")
def user_roblox_items():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    try:
        raw = rbx_get_tradable(u["roblox_id"], bot_id="1")
        out = []
        for itm in raw:
            tid = str(itm.get("itemTarget", {}).get("targetId", ""))
            itm["thumbnailUrl"] = rbx_item_thumb(tid) if tid else ""
            # Flatten per-instance hold info onto the item
            instances  = itm.get("instances", [])
            all_held   = instances and all(i.get("isOnHold", False) for i in instances)
            any_held   = any(i.get("isOnHold", False) for i in instances)
            itm["isOnHold"]    = all_held
            itm["anyOnHold"]   = any_held
            # Only keep the first non-held instance for display
            itm["instances"] = [i for i in instances if not i.get("isOnHold", False)]
            out.append(itm)
        return jsonify({"items": out})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    data              = request.json or {}
    item_ids          = data.get("item_ids", [])
    receive_target_id = data.get("receive_target_id", "lih")
    bot_id            = data.get("bot_id", "1")
    conn  = get_db()
    items = []
    for iid in item_ids:
        row = conn.execute(
            "SELECT * FROM inventory WHERE id=? AND user_id=?", (iid, u["id"])
        ).fetchone()
        if row:
            items.append(dict(row))
    if not items:
        conn.close()
        return jsonify({"error": "No valid items found"}), 400
    conn.commit()
    conn.close()
    # Send trade via trade_bot
    trade_result = {}
    try:
        r = http.get(f"{TRADE_BOT}/trade", params={
            "botid":            bot_id,
            "othersideuserid":  u["roblox_id"],
            "targetid":         items[0]["target_id"],
            "othersidetargetid": receive_target_id,
        }, timeout=60)
        trade_result = r.json()
    except Exception as e:
        print(f"[withdraw trade] {e}")
        trade_result = {"status": "error", "message": str(e)}

    if trade_result.get("status") == "error":
        msg = trade_result.get("message", "Trade failed")
        return jsonify({"error": f"Trade failed: {msg}", "log": trade_result.get("log", [])}), 500

    # Log successful withdrawal
    conn2 = get_db()
    for iid in item_ids:
        conn2.execute("DELETE FROM inventory WHERE id=? AND user_id=?", (iid, u["id"]))
    for item in items:
        conn2.execute(
            "INSERT INTO withdraw_history (user_id, item_name, target_id, rap, bot_id) VALUES (?,?,?,?,?)",
            (u["id"], item["item_name"], item["target_id"], item["rap"], bot_id)
        )
    conn2.commit()
    conn2.close()
    trade_url = f"https://www.roblox.com/users/{u['roblox_id']}/trade"
    return jsonify({"success": True, "trade_url": trade_url})

def _find_outbound_trade_id(user_roblox_id: str, bot_id: str) -> str | None:
    """After sending a trade, find the new outbound trade ID to this user."""
    cookie = roblox_cookie(bot_id)
    try:
        r = http.get(
            "https://trades.roblox.com/v1/trades/outbound?sortOrder=Desc&limit=10",
            headers={"Cookie": f".ROBLOSECURITY={cookie}", "Accept": "application/json"},
            timeout=10
        )
        for t in r.json().get("data", []):
            if str(t.get("user", {}).get("id", "")) == str(user_roblox_id):
                return str(t["id"])
    except Exception as e:
        print(f"[find trade id] {e}")
    return None


@app.route("/api/deposit/items/bulk", methods=["POST"])
def deposit_items_bulk():
    """Queue multiple item deposits - sends one trade per item."""
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    data  = request.json or {}
    items = data.get("items", [])   # [{target_id, item_name, bot_id?}, ...]
    if not items:
        return jsonify({"error": "No items specified"}), 400
    results, errors = [], []
    for itm in items:
        target_id = itm.get("target_id", "")
        item_name = itm.get("item_name", "")
        item_rap  = int(itm.get("rap", 0))

        bot_id, max_rap = _pick_bot_for_deposit(u["roblox_id"], item_rap)
        if bot_id is None:
            errors.append({"item": item_name, "error": "No bot available — all bot items are worth more than your deposit item."})
            continue

        trade_result = {}
        try:
            r = http.get(f"{TRADE_BOT}/trade", params={
                "botid":             bot_id,
                "othersideuserid":   u["roblox_id"],
                "targetid":          "lih",
                "othersidetargetid": target_id,
                "max_rap":           max_rap,
            }, timeout=60)
            trade_result = r.json()
        except Exception as e:
            trade_result = {"status": "error", "message": str(e)}

        if trade_result.get("status") == "error":
            errors.append({"item": item_name, "error": trade_result.get("message", "Trade failed")})
            continue

        time.sleep(2)
        trade_id = _find_outbound_trade_id(u["roblox_id"], bot_id)
        conn = get_db()
        conn.execute(
            "INSERT INTO pending_deposits (user_id, trade_id, bot_id, item_name, target_id) VALUES (?,?,?,?,?)",
            (u["id"], trade_id, bot_id, item_name, target_id)
        )
        conn.commit()
        conn.close()
        results.append({"target_id": target_id, "trade_id": trade_id})
        print(f"[bulk deposit] user={u['roblox_id']} item={item_name} bot={bot_id} trade_id={trade_id}")

    if not results and errors:
        return jsonify({"error": errors[0]["error"], "errors": errors}), 500
    return jsonify({"success": True, "results": results, "errors": errors,
                    "message": f"Sent {len(results)} deposit trade(s). Check your Roblox trades."})


def _pick_bot_for_deposit(user_roblox_id: str, item_rap: int) -> tuple[str, int] | tuple[None, None]:
    """
    Check every configured bot for a non-held item with RAP <= item_rap.
    Returns (bot_id, max_rap) for the first suitable bot, or (None, None).
    """
    cfg  = load_cfg()
    bots = list(cfg.get("bots", {}).keys())
    for bid in bots:
        try:
            uid  = rbx_get_uid(bid)
            raw  = rbx_get_tradable(uid, bid)
            avail = [
                itm for itm in raw
                if itm.get("recentAveragePrice") is not None
                and itm.get("instances")
                and all(not inst.get("isOnHold", False) for inst in itm.get("instances", []))
                and itm["recentAveragePrice"] <= item_rap
            ]
            if avail:
                return bid, item_rap
        except Exception as e:
            print(f"[pick_bot] bot={bid} error: {e}")
    return None, None


@app.route("/api/deposit/items", methods=["POST"])
def deposit_items():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    data      = request.json or {}
    target_id = data.get("target_id", "")
    item_name = data.get("item_name", "")
    item_rap  = int(data.get("rap", 0))

    # Find a bot with a suitable item (RAP <= deposit item RAP)
    bot_id, max_rap = _pick_bot_for_deposit(u["roblox_id"], item_rap)
    if bot_id is None:
        return jsonify({"error": "No bot available — all bot items are worth more than your deposit item. Deposit a higher value item."}), 400

    trade_result = {}
    try:
        r = http.get(f"{TRADE_BOT}/trade", params={
            "botid":             bot_id,
            "othersideuserid":   u["roblox_id"],
            "targetid":          "lih",
            "othersidetargetid": target_id,
            "max_rap":           max_rap,
        }, timeout=60)
        trade_result = r.json()
    except Exception as e:
        print(f"[deposit trade] {e}")
        trade_result = {"status": "error", "message": str(e)}

    if trade_result.get("status") == "error":
        msg = trade_result.get("message", "Trade failed")
        return jsonify({"error": msg, "log": trade_result.get("log", [])}), 500

    time.sleep(2)
    trade_id = _find_outbound_trade_id(u["roblox_id"], bot_id)
    conn = get_db()
    conn.execute(
        "INSERT INTO pending_deposits (user_id, trade_id, bot_id, item_name, target_id) VALUES (?,?,?,?,?)",
        (u["id"], trade_id, bot_id, item_name, target_id)
    )
    conn.commit()
    conn.close()
    print(f"[deposit] user={u['roblox_id']} trade_id={trade_id} bot={bot_id}")
    return jsonify({"success": True, "trade_id": trade_id,
                    "message": "Trade sent — your inventory will update automatically once accepted."})

# Chat

@app.route("/api/chat/history")
def chat_history():
    conn = get_db()
    rows = conn.execute("""
        SELECT cm.id, cm.user_id, cm.message, cm.created_at,
               u.roblox_name, u.roblox_id
        FROM chat_messages cm
        JOIN users u ON cm.user_id = u.id
        ORDER BY cm.created_at DESC LIMIT 60
    """).fetchall()
    conn.close()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["thumbnail"] = rbx_headshot(d["roblox_id"])
        out.append(d)
    return jsonify({"messages": out})

@app.route("/api/chat/tip", methods=["POST"])
def tip():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    data   = request.json or {}
    to_id  = data.get("to_user_id")
    amount = float(data.get("amount", 0))
    if to_id == u["id"]:
        return jsonify({"error": "Cannot tip yourself"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400
    conn = get_db()
    bal  = conn.execute("SELECT balance FROM users WHERE id=?", (u["id"],)).fetchone()["balance"]
    if bal < amount:
        conn.close()
        return jsonify({"error": "Insufficient balance"}), 400
    target = conn.execute("SELECT * FROM users WHERE id=?", (to_id,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "User not found"}), 404
    conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount, u["id"]))
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, to_id))
    conn.commit()
    new_bal = conn.execute("SELECT balance FROM users WHERE id=?", (u["id"],)).fetchone()["balance"]
    conn.close()
    socketio.emit("toast", {
        "type":    "success",
        "message": f"You received a ${amount:.2f} tip from {u['roblox_name']}!",
    }, room=f"u{to_id}")
    socketio.emit("balance_update", {"balance": round(new_bal, 4)}, room=f"u{u['id']}")
    socketio.emit("chat_tip", {
        "from": u["roblox_name"],
        "to":   target["roblox_name"],
        "amount": amount,
    })
    return jsonify({"success": True, "balance": round(new_bal, 4)})

# Socket.IO

@socketio.on("connect")
def on_connect():
    u = current_user()
    if u:
        join_room(f"u{u['id']}")
        emit("connected", {"user_id": u["id"]})

@socketio.on("chat_send")
def on_chat(data):
    u   = current_user()
    if not u:
        return
    msg = str(data.get("message", "")).strip()[:200]
    if not msg:
        return
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_messages (user_id, message) VALUES (?,?)", (u["id"], msg)
    )
    conn.commit()
    conn.close()
    socketio.emit("chat_message", {
        "user_id":   u["id"],
        "username":  u["roblox_name"],
        "thumbnail": rbx_headshot(u["roblox_id"]),
        "message":   msg,
        "time":      datetime.now().strftime("%H:%M"),
    })


def _fetch_trade_data(trade_id: str, bot_id: str = "1") -> dict:
    """
    Fetch a single trade via the trade_bot browser (uses all session cookies).
    Falls back to trying every bot if the specified one fails.
    """
    cfg  = load_cfg()
    bots = list(cfg.get("bots", {}).keys())
    # Try the specified bot first, then all others
    order = [bot_id] + [b for b in bots if b != bot_id]
    last_err = None
    for bid in order:
        try:
            r = http.get(
                f"{TRADE_BOT}/check_trade",
                params={"tradeid": trade_id, "botid": bid},
                timeout=15
            )
            data = r.json()
            if "error" in data:
                last_err = data["error"]
                continue
            return data
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"Could not fetch trade {trade_id}: {last_err}")


def _extract_user_items_from_trade(data: dict, uid_int: int) -> list:
    """
    Given a Roblox trade response, return the list of items offered by uid_int.
    Handles the participantAOffer / participantBOffer format.
    """
    for key in ("participantAOffer", "participantBOffer"):
        offer = data.get(key, {})
        if offer.get("user", {}).get("id") == uid_int:
            return offer.get("items", [])
    return []


def _credit_deposit(conn, dep, data: dict) -> int:
    """
    Credit a completed deposit trade to the user's inventory.
    Returns the number of items credited.
    """
    user    = conn.execute("SELECT * FROM users WHERE id=?", (dep["user_id"],)).fetchone()
    uid_int = int(user["roblox_id"])
    items   = _extract_user_items_from_trade(data, uid_int)
    for item in items:
        tid   = str(item.get("itemTarget", {}).get("targetId", ""))
        name  = item.get("itemName") or dep["item_name"] or "Unknown"
        rap   = item.get("recentAveragePrice", 0)
        thumb = rbx_item_thumb(tid)
        conn.execute(
            "INSERT INTO inventory (user_id,item_name,target_id,rap,thumbnail_url) VALUES (?,?,?,?,?)",
            (dep["user_id"], name, tid, rap, thumb)
        )
    conn.execute(
        "UPDATE pending_deposits SET status='completed', completed_at=datetime('now') WHERE id=?",
        (dep["id"],)
    )
    conn.commit()
    return len(items)


def monitor_deposits():
    """Poll pending deposit trades every 15s and credit inventory on completion."""
    while True:
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM pending_deposits WHERE status IN ('pending','failed') AND trade_id IS NOT NULL"
            ).fetchall()
            for dep in rows:
                try:
                    data   = _fetch_trade_data(dep["trade_id"], dep["bot_id"])
                    status = data.get("status", "")
                    print(f"[deposit poll] trade={dep['trade_id']} status={status}")

                    if status in ("Completed", "Accepted"):
                        n = _credit_deposit(conn, dep, data)
                        socketio.emit("toast", {
                            "type":    "success",
                            "message": f"Deposit confirmed! {n} item(s) added to your inventory.",
                        }, room=f"u{dep['user_id']}")
                        socketio.emit("inventory_updated", {}, room=f"u{dep['user_id']}")

                    elif status in ("Declined", "Expired", "RejectedDueToError", "Inactive"):
                        conn.execute(
                            "UPDATE pending_deposits SET status='failed', completed_at=datetime('now') WHERE id=?",
                            (dep["id"],)
                        )
                        conn.commit()
                        socketio.emit("toast", {
                            "type":    "error",
                            "message": f"Deposit trade {status.lower()} — nothing was added.",
                        }, room=f"u{dep['user_id']}")
                        socketio.emit("inventory_updated", {}, room=f"u{dep['user_id']}")

                except Exception as e:
                    print(f"[deposit poll trade={dep['trade_id']}] {e}")
            conn.close()
        except Exception as e:
            print(f"[deposit monitor] {e}")
        time.sleep(15)


ADMIN_PASSWORD = "admin123"
ADMIN_SESSION_KEY = "rbxlim_admin"

def admin_authed():
    return session.get(ADMIN_SESSION_KEY) is True

def ltc_sweep(dest_address: str) -> dict:
    """Derive private keys and sweep all deposit addresses to dest_address."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from bitcoinlib.mnemonic import Mnemonic as BM
        from bitcoinlib.keys import HDKey
        from bitcoinlib.transactions import Transaction, Input, Output
    conn = get_db()
    rows = conn.execute(
        "SELECT user_id, address, derivation_index, monitored_balance FROM ltc_addresses"
    ).fetchall()
    conn.close()
    mnemonic = ensure_ltc_mnemonic()
    results  = []
    total    = 0.0
    for row in rows:
        bal = row["monitored_balance"]
        if bal < 0.0001:
            continue
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from bitcoinlib.mnemonic import Mnemonic as BM
                from bitcoinlib.keys import HDKey
                seed  = BM().to_seed(mnemonic)
                root  = HDKey.from_seed(seed, network="litecoin")
                child = root.subkey_for_path(f"m/44'/2'/0'/0/{row['derivation_index']}")
                wif   = child.wif()
            # use BlockCypher to build + broadcast TX
            r = http.post(
                f"https://api.blockcypher.com/v1/ltc/main/txs/new",
                json={
                    "inputs":  [{"addresses": [row["address"]]}],
                    "outputs": [{"addresses": [dest_address], "value": int(bal * 1e8 * 0.99)}],
                },
                timeout=15
            )
            tx_data = r.json()
            if "errors" in tx_data:
                results.append({"address": row["address"], "error": str(tx_data["errors"])})
                continue
            # sign
            import hashlib as hl
            from ecdsa import SigningKey, SECP256k1
            sk = SigningKey.from_string(bytes.fromhex(child.private_hex()), curve=SECP256k1)
            tosign = tx_data.get("tosign", [])
            sigs   = [sk.sign_digest(bytes.fromhex(t), sigencode=lambda r,s,o: r.to_bytes(32,'big')+s.to_bytes(32,'big')).hex() for t in tosign]
            pubkeys = [child.public_hex()] * len(tosign)
            # send signed
            r2 = http.post(
                "https://api.blockcypher.com/v1/ltc/main/txs/send",
                json={**tx_data, "signatures": sigs, "pubkeys": pubkeys},
                timeout=15
            )
            r2_data = r2.json()
            total  += bal
            results.append({"address": row["address"], "ltc": bal, "tx": r2_data.get("tx", {}).get("hash", "")})
        except Exception as e:
            results.append({"address": row["address"], "error": str(e)})
    return {"swept_ltc": total, "results": results}


@app.route("/ap", methods=["GET"])
def admin_panel():
    if not admin_authed():
        return '''<!DOCTYPE html><html><head><title>Admin</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#080810;color:#e2e8f0;font-family:Inter,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh}
.card{background:#13132a;border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:40px;width:340px}
h2{margin-bottom:24px;font-size:1.3rem;color:#f0c040}
input{width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:11px 14px;color:#e2e8f0;font-size:.95rem;margin-bottom:14px;outline:none}
button{width:100%;padding:12px;background:linear-gradient(135deg,#8b5cf6,#6d28d9);border:none;border-radius:8px;color:#fff;font-size:.95rem;font-weight:600;cursor:pointer}
.err{color:#ef4444;font-size:.82rem;margin-bottom:10px;display:none}</style></head>
<body><div class="card"><h2>Admin Panel</h2>
<div class="err" id="err">Wrong password</div>
<input type="password" id="pw" placeholder="Password" onkeydown="if(event.key==='Enter')login()">
<button onclick="login()">Login</button></div>
<script>async function login(){const r=await fetch('/ap/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});const d=await r.json();if(d.ok)location.reload();else{document.getElementById('err').style.display='block';}}</script>
</body></html>'''
    return '''<!DOCTYPE html><html><head><title>RBXLIM Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080810;color:#e2e8f0;font-family:'Inter',sans-serif;min-height:100vh;padding:32px}
h1{font-size:1.5rem;font-weight:700;color:#f0c040;margin-bottom:4px}
.sub{color:#64748b;font-size:.85rem;margin-bottom:32px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:32px}
.stat{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:20px}
.stat-label{font-size:.78rem;color:#64748b;margin-bottom:6px}
.stat-value{font-size:1.6rem;font-weight:700;color:#f0c040}
.section{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:24px;margin-bottom:24px}
.section h2{font-size:1rem;font-weight:600;margin-bottom:16px;color:#94a3b8}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:.78rem;color:#64748b;padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.06)}
td{padding:10px 12px;font-size:.85rem;border-bottom:1px solid rgba(255,255,255,0.04)}
tr:last-child td{border-bottom:none}
.badge{padding:2px 8px;border-radius:20px;font-size:.7rem;font-weight:600;background:rgba(34,197,94,0.15);color:#22c55e}
.withdraw-box{display:flex;gap:10px;align-items:center}
input.ltc-in{flex:1;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:10px 14px;color:#e2e8f0;font-size:.9rem;outline:none}
.btn{padding:10px 20px;border:none;border-radius:8px;font-size:.88rem;font-weight:600;cursor:pointer;font-family:inherit}
.btn-gold{background:linear-gradient(135deg,#f0c040,#d97706);color:#000}
.btn-red{background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.3)}
.result{margin-top:14px;background:rgba(255,255,255,0.04);border-radius:8px;padding:12px;font-size:.82rem;font-family:monospace;white-space:pre-wrap;display:none}
.loading{color:#64748b;font-size:.85rem}
</style></head>
<body>
<h1>RBXLIM Admin</h1>
<div class="sub">Platform management</div>
<div class="stats" id="stats">
  <div class="stat"><div class="stat-label">Total Users</div><div class="stat-value" id="s-users">—</div></div>
  <div class="stat"><div class="stat-label">Total Balances</div><div class="stat-value" id="s-bal">—</div></div>
  <div class="stat"><div class="stat-label">Total LTC Received</div><div class="stat-value" id="s-ltc">—</div></div>
  <div class="stat"><div class="stat-label">LTC Price</div><div class="stat-value" id="s-price">—</div></div>
</div>
<div class="section">
  <h2>Retroactive Deposit Credit</h2>
  <div style="margin-bottom:14px">
    <div style="font-size:.82rem;color:#94a3b8;margin-bottom:10px">Credit a specific old trade by ID (works across deployments):</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:8px">
      <div><label style="font-size:.75rem;color:#64748b;display:block;margin-bottom:4px">Trade ID</label>
        <input id="retro-trade-id" placeholder="e.g. 3875848840455886" style="background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px 12px;color:#e2e8f0;font-size:.85rem;outline:none;width:220px"></div>
      <div><label style="font-size:.75rem;color:#64748b;display:block;margin-bottom:4px">User</label>
        <select id="retro-user" style="background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px 12px;color:#e2e8f0;font-size:.85rem;outline:none"></select></div>
      <div><label style="font-size:.75rem;color:#64748b;display:block;margin-bottom:4px">Bot ID</label>
        <input id="retro-bot" value="1" style="width:60px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px;color:#e2e8f0;font-size:.85rem;outline:none"></div>
      <button class="btn btn-gold" style="padding:8px 16px;font-size:.85rem" onclick="doRetroDeposit()">Credit Trade</button>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn" style="padding:8px 16px;font-size:.85rem;background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid rgba(34,197,94,0.3)" onclick="doReprocessAll(false)">Re-check All Pending</button>
      <button class="btn" style="padding:8px 16px;font-size:.85rem;background:rgba(139,92,246,0.15);color:#8b5cf6;border:1px solid rgba(139,92,246,0.3)" onclick="doReprocessAll(true)">Re-check Pending + Failed</button>
    </div>
    <div class="result" id="retro-result"></div>
  </div>
</div>
<div class="section">
  <h2>Withdraw LTC to Address</h2>
  <div class="withdraw-box">
    <input class="ltc-in" id="dest-addr" placeholder="Litecoin address (ltc1q...)">
    <button class="btn btn-gold" onclick="doWithdraw()">Sweep All LTC</button>
    <button class="btn" style="padding:10px 20px;background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid rgba(34,197,94,0.3);border-radius:8px;font-size:.88rem;font-weight:600;cursor:pointer" onclick="doRescan()">Re-scan & Credit LTC</button>
  </div>
  <div class="result" id="sweep-result"></div>
</div>
<div class="section">
  <h2>Users & Balances</h2>
  <table><thead><tr><th>Roblox Name</th><th>Roblox ID</th><th>Balance (USD)</th><th>LTC Address</th><th>LTC Received</th><th>Actions</th></tr></thead>
  <tbody id="users-table"><tr><td colspan="6" class="loading">Loading...</td></tr></tbody></table>
</div>
<div class="section">
  <h2>Inventory Management</h2>
  <div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center">
    <select id="inv-user-sel" style="background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px 12px;color:#e2e8f0;font-size:.85rem;outline:none">
      <option value="">All users</option>
    </select>
    <button class="btn btn-gold" style="padding:8px 16px;font-size:.85rem" onclick="loadInventory()">Load Inventory</button>
    <button class="btn" style="padding:8px 16px;font-size:.85rem;background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid rgba(34,197,94,0.3)" onclick="showAddItemForm()">+ Add Item</button>
  </div>
  <div id="add-item-form" style="display:none;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:16px;margin-bottom:16px">
    <div style="font-size:.85rem;font-weight:600;color:#94a3b8;margin-bottom:10px">Add Item to Inventory</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 80px auto;gap:8px;align-items:end">
      <div><label style="font-size:.75rem;color:#64748b;display:block;margin-bottom:4px">User</label>
        <select id="add-item-user" style="width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px;color:#e2e8f0;font-size:.82rem;outline:none"></select></div>
      <div><label style="font-size:.75rem;color:#64748b;display:block;margin-bottom:4px">Item Name</label>
        <input id="add-item-name" placeholder="e.g. Domino Crown" style="width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px;color:#e2e8f0;font-size:.82rem;outline:none"></div>
      <div><label style="font-size:.75rem;color:#64748b;display:block;margin-bottom:4px">Target ID</label>
        <input id="add-item-tid" placeholder="Roblox asset ID" style="width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px;color:#e2e8f0;font-size:.82rem;outline:none"></div>
      <div><label style="font-size:.75rem;color:#64748b;display:block;margin-bottom:4px">RAP</label>
        <input id="add-item-rap" type="number" placeholder="0" style="width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px;color:#e2e8f0;font-size:.82rem;outline:none"></div>
      <button class="btn btn-gold" style="padding:8px 16px;font-size:.82rem" onclick="doAddItem()">Add</button>
    </div>
  </div>
  <table><thead><tr><th>User</th><th>Item</th><th>Target ID</th><th>RAP</th><th>Added</th><th>Remove</th></tr></thead>
  <tbody id="inv-table"><tr><td colspan="6" class="loading">Select a filter and click Load</td></tr></tbody></table>
</div>
<div class="section">
  <h2>Deposit Addresses</h2>
  <table><thead><tr><th>Address</th><th>LTC Received</th><th>USD Value</th></tr></thead>
  <tbody id="addr-table"><tr><td colspan="3" class="loading">Loading...</td></tr></tbody></table>
</div>
<button class="btn btn-red" onclick="logout()" style="margin-top:8px">Logout</button>
<script>
let allUsers=[];
async function load(){
  const r=await fetch('/ap/data');
  const d=await r.json();
  document.getElementById('s-users').textContent=d.total_users;
  document.getElementById('s-bal').textContent='$'+d.total_balance.toFixed(2);
  document.getElementById('s-ltc').textContent=d.total_ltc_received.toFixed(6)+' LTC';
  document.getElementById('s-price').textContent='$'+d.ltc_price.toFixed(2);
  allUsers=d.users;
  document.getElementById('users-table').innerHTML=d.users.map(u=>`
    <tr>
      <td><strong>${u.roblox_name}</strong></td>
      <td style="color:#64748b">${u.roblox_id}</td>
      <td style="color:#f0c040;font-weight:600">$${u.balance.toFixed(2)}</td>
      <td style="font-family:monospace;font-size:.75rem;color:#8b5cf6">${u.ltc_address||'—'}</td>
      <td>${u.ltc_received?u.ltc_received.toFixed(6)+' LTC':'—'}</td>
      <td style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn" style="padding:3px 8px;font-size:.72rem;background:rgba(240,192,64,0.15);color:#f0c040;border:1px solid rgba(240,192,64,0.3)" onclick="manualCredit(${u.id},'${u.roblox_name}',1)">+ Credit</button>
        <button class="btn" style="padding:3px 8px;font-size:.72rem;background:rgba(239,68,68,0.12);color:#ef4444;border:1px solid rgba(239,68,68,0.3)" onclick="manualCredit(${u.id},'${u.roblox_name}',-1)">- Debit</button>
      </td>
    </tr>`).join('');
  document.getElementById('addr-table').innerHTML=d.addresses.map(a=>`
    <tr>
      <td style="font-family:monospace;font-size:.78rem">${a.address}</td>
      <td>${a.monitored_balance.toFixed(6)} LTC</td>
      <td style="color:#f0c040">$${(a.monitored_balance*d.ltc_price).toFixed(2)}</td>
    </tr>`).join('');
  // populate user dropdowns
  const opts=d.users.map(u=>`<option value="${u.id}">${u.roblox_name}</option>`).join('');
  document.getElementById('inv-user-sel').innerHTML='<option value="">All users</option>'+opts;
  document.getElementById('add-item-user').innerHTML=opts;
  document.getElementById('retro-user').innerHTML=opts;
}
async function manualCredit(userId, name, sign){
  const label = sign>0?'credit':'debit';
  const amt = prompt(`Manual ${label} for ${name}\nEnter USD amount:`);
  if(!amt || isNaN(parseFloat(amt)) || parseFloat(amt)<=0) return;
  const amount = sign * parseFloat(amt);
  const r = await fetch('/ap/credit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:userId,amount})});
  const d = await r.json();
  if(d.ok){alert(`${sign>0?'Credited':'Debited'} $${parseFloat(amt).toFixed(2)} ${sign>0?'to':'from'} ${name}. New balance: $${d.new_balance}`);load();}
  else alert('Error: '+d.error);
}
async function loadInventory(){
  const uid=document.getElementById('inv-user-sel').value;
  const url='/ap/inventory/list'+(uid?'?user_id='+uid:'');
  const r=await fetch(url);
  const d=await r.json();
  document.getElementById('inv-table').innerHTML=d.items.length?d.items.map(i=>`
    <tr>
      <td>${i.roblox_name}</td>
      <td><strong>${i.item_name}</strong></td>
      <td style="font-family:monospace;font-size:.75rem">${i.target_id||'—'}</td>
      <td style="color:#f0c040">${i.rap?.toLocaleString()||'—'}</td>
      <td style="font-size:.75rem;color:#64748b">${(i.acquired_at||'').slice(0,16)}</td>
      <td><button class="btn btn-red" style="padding:3px 8px;font-size:.72rem" onclick="removeItem(${i.id})">Remove</button></td>
    </tr>`).join(''):'<tr><td colspan="6" style="color:#64748b;padding:16px">No items found</td></tr>';
}
function showAddItemForm(){
  const f=document.getElementById('add-item-form');
  f.style.display=f.style.display==='none'?'block':'none';
}
async function doAddItem(){
  const uid=document.getElementById('add-item-user').value;
  const name=document.getElementById('add-item-name').value.trim();
  const tid=document.getElementById('add-item-tid').value.trim();
  const rap=parseInt(document.getElementById('add-item-rap').value)||0;
  if(!name||!tid){alert('Item name and Target ID required');return;}
  const r=await fetch('/ap/inventory/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:parseInt(uid),item_name:name,target_id:tid,rap})});
  const d=await r.json();
  if(d.ok){alert('Item added!');document.getElementById('add-item-name').value='';document.getElementById('add-item-tid').value='';document.getElementById('add-item-rap').value='';loadInventory();}
  else alert('Error: '+d.error);
}
async function removeItem(itemId){
  if(!confirm('Remove this item from inventory?'))return;
  const r=await fetch('/ap/inventory/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:itemId})});
  const d=await r.json();
  if(d.ok)loadInventory();
  else alert('Error: '+d.error);
}
async function doRetroDeposit(){
  const tid=document.getElementById('retro-trade-id').value.trim();
  const uid=document.getElementById('retro-user').value;
  const bid=document.getElementById('retro-bot').value.trim()||'1';
  if(!tid||!uid){alert('Trade ID and User required');return;}
  const res=document.getElementById('retro-result');
  res.style.display='block';res.textContent='Looking up trade...';
  const r=await fetch('/ap/reprocess-deposit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trade_id:tid,user_id:parseInt(uid),bot_id:bid})});
  const d=await r.json();
  if(d.ok) res.textContent=`Credited! ${d.items_credited} item(s) added. Trade status: ${d.trade_status}`;
  else res.textContent='Error: '+(d.error||JSON.stringify(d));
}
async function doReprocessAll(includeFailed){
  const res=document.getElementById('retro-result');
  res.style.display='block';res.textContent='Re-checking all trades...';
  const r=await fetch('/ap/reprocess-all-pending',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({include_failed:includeFailed})});
  const d=await r.json();
  const lines=[];
  if(d.credited?.length) lines.push(`Credited ${d.credited.length} trade(s):\n`+d.credited.map(c=>`  trade=${c.trade_id} items=${c.items}`).join('\n'));
  if(d.skipped?.length) lines.push(`Skipped ${d.skipped.length} (not completed yet)`);
  if(d.errors?.length) lines.push(`Errors:\n`+d.errors.map(e=>`  ${e.trade_id}: ${e.error}`).join('\n'));
  res.textContent=lines.join('\n')||'Nothing to process.';
  load();
}
async function doRescan(){
  const res=document.getElementById('sweep-result');
  res.style.display='block';res.textContent='Scanning all LTC addresses...';
  const r=await fetch('/ap/rescan-ltc',{method:'POST'});
  const d=await r.json();
  if(d.credited&&d.credited.length)
    res.textContent='Credited:\n'+d.credited.map(c=>`  ${c.user}: +$${c.usd} (${c.ltc.toFixed(8)} LTC)`).join('\n');
  else res.textContent='No new balance found (all addresses up to date).';
  load();
}
async function doWithdraw(){
  const addr=document.getElementById('dest-addr').value.trim();
  if(!addr){alert('Enter a LTC address');return;}
  if(!confirm('Sweep all LTC deposits to '+addr+'?'))return;
  const res=document.getElementById('sweep-result');
  res.style.display='block';res.textContent='Sweeping...';
  const r=await fetch('/ap/withdraw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address:addr})});
  const d=await r.json();
  res.textContent=JSON.stringify(d,null,2);
  load();
}
async function logout(){await fetch('/ap/logout',{method:'POST'});location.reload();}
load();
</script>
</body></html>'''


@app.route("/ap/login", methods=["POST"])
def admin_login():
    pw = (request.json or {}).get("password", "")
    if pw == ADMIN_PASSWORD:
        session[ADMIN_SESSION_KEY] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401


@app.route("/ap/logout", methods=["POST"])
def admin_logout():
    session.pop(ADMIN_SESSION_KEY, None)
    return jsonify({"ok": True})


@app.route("/ap/data")
def admin_data():
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    users = conn.execute("""
        SELECT u.id, u.roblox_id, u.roblox_name, u.balance,
               la.address as ltc_address, la.monitored_balance as ltc_received
        FROM users u
        LEFT JOIN ltc_addresses la ON la.user_id = u.id
        WHERE u.roblox_id != '__BOT__'
        ORDER BY u.balance DESC
    """).fetchall()
    addrs = conn.execute(
        "SELECT address, monitored_balance FROM ltc_addresses ORDER BY monitored_balance DESC"
    ).fetchall()
    conn.close()
    total_bal = sum(r["balance"] for r in users)
    total_ltc = sum(r["monitored_balance"] for r in addrs)
    ltc_price = get_ltc_price()
    return jsonify({
        "total_users":        len(users),
        "total_balance":      round(total_bal, 4),
        "total_ltc_received": round(total_ltc, 8),
        "ltc_price":          ltc_price,
        "users":    [dict(r) for r in users],
        "addresses":[dict(r) for r in addrs],
    })


@app.route("/ap/withdraw", methods=["POST"])
def admin_withdraw():
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    dest = (request.json or {}).get("address", "").strip()
    if not dest:
        return jsonify({"error": "Address required"}), 400
    result = ltc_sweep(dest)
    return jsonify(result)


@app.route("/ap/credit", methods=["POST"])
def admin_credit():
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    data    = request.json or {}
    user_id = data.get("user_id")
    amount  = float(data.get("amount", 0))
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = get_db()
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, user_id))
    conn.commit()
    user = conn.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    new_bal = round(user["balance"], 4) if user else 0
    if amount > 0:
        socketio.emit("balance_update", {"balance": new_bal, "added": amount}, room=f"u{user_id}")
        socketio.emit("toast", {"type": "success", "message": f"Admin credited ${amount:.2f} to your balance"}, room=f"u{user_id}")
    return jsonify({"ok": True, "new_balance": new_bal})


@app.route("/ap/inventory/list")
def admin_inventory_list():
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    user_id = request.args.get("user_id")
    conn    = get_db()
    if user_id:
        items = conn.execute(
            "SELECT i.*, u.roblox_name FROM inventory i JOIN users u ON i.user_id=u.id WHERE i.user_id=? ORDER BY i.acquired_at DESC",
            (user_id,)
        ).fetchall()
    else:
        items = conn.execute(
            "SELECT i.*, u.roblox_name FROM inventory i JOIN users u ON i.user_id=u.id ORDER BY i.acquired_at DESC LIMIT 200"
        ).fetchall()
    conn.close()
    return jsonify({"items": [dict(i) for i in items]})


@app.route("/ap/inventory/add", methods=["POST"])
def admin_inventory_add():
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    data      = request.json or {}
    user_id   = data.get("user_id")
    item_name = data.get("item_name", "").strip()
    target_id = data.get("target_id", "").strip()
    rap       = int(data.get("rap", 0))
    if not all([user_id, item_name, target_id]):
        return jsonify({"error": "user_id, item_name, target_id required"}), 400
    thumb = rbx_item_thumb(target_id)
    conn  = get_db()
    conn.execute(
        "INSERT INTO inventory (user_id,item_name,target_id,rap,thumbnail_url) VALUES (?,?,?,?,?)",
        (user_id, item_name, target_id, rap, thumb)
    )
    conn.commit()
    conn.close()
    socketio.emit("inventory_updated", {}, room=f"u{user_id}")
    return jsonify({"ok": True})


@app.route("/ap/inventory/remove", methods=["POST"])
def admin_inventory_remove():
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    item_id = (request.json or {}).get("item_id")
    if not item_id:
        return jsonify({"error": "item_id required"}), 400
    conn = get_db()
    row  = conn.execute("SELECT user_id FROM inventory WHERE id=?", (item_id,)).fetchone()
    conn.execute("DELETE FROM inventory WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    if row:
        socketio.emit("inventory_updated", {}, room=f"u{row['user_id']}")
    return jsonify({"ok": True})


@app.route("/ap/reprocess-deposit", methods=["POST"])
def admin_reprocess_deposit():
    """
    Retroactively credit a deposit by trade_id.
    Works for old deployments — looks up the trade and credits items to the user.
    Body: {trade_id, user_id, bot_id?}
    """
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    data     = request.json or {}
    trade_id = str(data.get("trade_id", "")).strip()
    user_id  = data.get("user_id")
    bot_id   = str(data.get("bot_id", "1"))
    if not trade_id or not user_id:
        return jsonify({"error": "trade_id and user_id required"}), 400
    conn = get_db()
    # Upsert a pending_deposits row so _credit_deposit can use it
    existing = conn.execute(
        "SELECT * FROM pending_deposits WHERE trade_id=?", (trade_id,)
    ).fetchone()
    if existing:
        dep = dict(existing)
        if dep["status"] == "completed":
            conn.close()
            return jsonify({"error": "Already credited", "status": "completed"})
        # reset to pending so it can be credited
        conn.execute("UPDATE pending_deposits SET status='pending', user_id=?, bot_id=? WHERE id=?",
                     (user_id, bot_id, dep["id"]))
        conn.commit()
        dep = dict(conn.execute("SELECT * FROM pending_deposits WHERE id=?", (dep["id"],)).fetchone())
    else:
        cur = conn.execute(
            "INSERT INTO pending_deposits (user_id, trade_id, bot_id, status) VALUES (?,?,?,'pending')",
            (user_id, trade_id, bot_id)
        )
        conn.commit()
        dep = dict(conn.execute("SELECT * FROM pending_deposits WHERE id=?", (cur.lastrowid,)).fetchone())
    try:
        trade_data = _fetch_trade_data(trade_id, bot_id)
        status     = trade_data.get("status", "")
        print(f"[admin reprocess] trade={trade_id} status={status}")
        if status not in ("Completed", "Accepted"):
            conn.execute("UPDATE pending_deposits SET status='failed' WHERE id=?", (dep["id"],))
            conn.commit()
            conn.close()
            return jsonify({"error": f"Trade status is '{status}', not Completed"}), 400
        n = _credit_deposit(conn, dep, trade_data)
        conn.close()
        socketio.emit("toast", {
            "type": "success",
            "message": f"Your deposit was manually credited — {n} item(s) added.",
        }, room=f"u{user_id}")
        socketio.emit("inventory_updated", {}, room=f"u{user_id}")
        return jsonify({"ok": True, "items_credited": n, "trade_status": status})
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/ap/reprocess-all-pending", methods=["POST"])
def admin_reprocess_all_pending():
    """Re-check every pending (or failed) deposit trade and credit if completed."""
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    include_failed = (request.json or {}).get("include_failed", False)
    conn  = get_db()
    query = "SELECT * FROM pending_deposits WHERE trade_id IS NOT NULL AND status IN ('pending'"
    if include_failed:
        query += ", 'failed'"
    query += ")"
    rows = conn.execute(query).fetchall()
    credited, skipped, errors = [], [], []
    for dep in rows:
        dep = dict(dep)
        try:
            trade_data = _fetch_trade_data(dep["trade_id"], dep["bot_id"])
            status     = trade_data.get("status", "")
            if status in ("Completed", "Accepted"):
                n = _credit_deposit(conn, dep, trade_data)
                credited.append({"trade_id": dep["trade_id"], "user_id": dep["user_id"], "items": n})
                socketio.emit("toast", {"type": "success",
                    "message": f"Deposit retroactively credited — {n} item(s) added."}, room=f"u{dep['user_id']}")
                socketio.emit("inventory_updated", {}, room=f"u{dep['user_id']}")
            else:
                skipped.append({"trade_id": dep["trade_id"], "status": status})
        except Exception as e:
            errors.append({"trade_id": dep["trade_id"], "error": str(e)})
    conn.close()
    return jsonify({"credited": credited, "skipped": skipped, "errors": errors})


@app.route("/ap/rescan-ltc", methods=["POST"])
def admin_rescan_ltc():
    """Force re-check all LTC addresses and credit any uncredited balance."""
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    conn  = get_db()
    rows  = conn.execute("SELECT user_id, address, monitored_balance FROM ltc_addresses").fetchall()
    price = get_ltc_price()
    credited = []
    for row in rows:
        total = check_ltc_received(row["address"])
        if total > row["monitored_balance"] + 1e-9:
            new_ltc = total - row["monitored_balance"]
            usd     = round(new_ltc * price, 4)
            conn.execute("UPDATE ltc_addresses SET monitored_balance=? WHERE user_id=?", (total, row["user_id"]))
            conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (usd, row["user_id"]))
            conn.commit()
            user = conn.execute("SELECT roblox_name, balance FROM users WHERE id=?", (row["user_id"],)).fetchone()
            socketio.emit("balance_update", {"balance": round(user["balance"], 4), "added": usd}, room=f"u{row['user_id']}")
            socketio.emit("toast", {"type": "success", "message": f"Credited ${usd:.2f} ({new_ltc:.6f} LTC)"}, room=f"u{row['user_id']}")
            credited.append({"address": row["address"], "user": user["roblox_name"], "usd": usd, "ltc": new_ltc})
    conn.close()
    return jsonify({"credited": credited, "ltc_price": price})


@app.route("/ap/users")
def admin_users_list():
    if not admin_authed():
        return jsonify({"error": "Unauthorized"}), 401
    conn  = get_db()
    users = conn.execute(
        "SELECT id, roblox_id, roblox_name, balance FROM users WHERE roblox_id != '__BOT__' ORDER BY roblox_name"
    ).fetchall()
    conn.close()
    return jsonify({"users": [dict(u) for u in users]})


# ── Trade history ─────────────────────────────────────────────

@app.route("/api/deposit/history")
def deposit_history_api():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pending_deposits WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (u["id"],)
    ).fetchall()
    conn.close()
    return jsonify({"history": [dict(r) for r in rows]})


@app.route("/api/withdraw/history")
def withdraw_history_api():
    u = current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM withdraw_history WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (u["id"],)
    ).fetchall()
    conn.close()
    return jsonify({"history": [dict(r) for r in rows]})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=monitor_ltc,      daemon=True).start()
    threading.Thread(target=monitor_deposits,  daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    print(f"[*] RBXLIM running on http://0.0.0.0:{port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
