import json
import os
import sys
import time
import threading
import asyncio
import pyotp
from aiohttp import web
from playwright.async_api import async_playwright

CONFIG_FILE = "config.json"

# g_bots: { bot_id: { "context": BrowserContext, "secret_key": str } }
g_bots = {}


# ── Config ───────────────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"bots": {}}


def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def setup_bot(config: dict, bot_id: str) -> dict:
    bots = config.setdefault("bots", {})
    bot  = bots.setdefault(bot_id, {})

    print(f"\n── Setting up bot {bot_id} ──")
    bot["secret_key"]    = input("  Authenticator Secret Key: ").strip()

    print("  Get .ROBLOSECURITY: F12 → Application → Cookies → roblox.com")
    bot["roblosecurity"] = input("  .ROBLOSECURITY value: ").strip()

    print("  Get .rbxsession from the same place")
    bot["rbxsession"]    = input("  .rbxsession value: ").strip()

    bots[bot_id] = bot
    save_config(config)
    print(f"[+] Bot {bot_id} saved.")
    return config


# ── TOTP printer ─────────────────────────────────────────────

def totp_printer(bot_id: str, secret_key: str):
    totp      = pyotp.TOTP(secret_key)
    last_code = None
    while True:
        code      = totp.now()
        remaining = 30 - int(time.time()) % 30
        if code != last_code:
            print(f"\n[TOTP bot={bot_id}] {code}  ({remaining}s left)")
            last_code = code
        time.sleep(1)


# ── Trade helpers ────────────────────────────────────────────

async def fetch_lowest_item(page, user_id: str, max_rap: int = 0):
    """
    Returns the target_id of the lowest non-held item.
    If max_rap > 0, only considers items with RAP <= max_rap.
    Returns None if no suitable item found.
    """
    data = await page.evaluate(f"""async () => {{
        const r = await fetch(
            'https://trades.roblox.com/v2/users/{user_id}/tradableitems?sortBy=CreationTime&cursor=&limit=50&sortOrder=Desc',
            {{ credentials: 'include', headers: {{ Accept: 'application/json' }} }}
        );
        return await r.json();
    }}""")
    items     = data.get("items", [])
    available = [
        i for i in items
        if i.get("recentAveragePrice") is not None
        and i.get("instances")
        and all(not inst.get("isOnHold", False) for inst in i.get("instances", []))
    ]
    if not available:
        return None
    if max_rap > 0:
        under = [i for i in available if i["recentAveragePrice"] <= max_rap]
        if not under:
            return "NO_BOT_ITEM"   # caller will surface this as an error
        available = under
    lowest = min(available, key=lambda i: i["recentAveragePrice"])
    return str(lowest["itemTarget"]["targetId"])


async def do_trade(bot_id: str, other_user_id: str,
                   local_target_id: str, other_target_id: str,
                   max_rap: int = 0) -> dict:
    if bot_id not in g_bots:
        return {"status": "error", "message": f"Bot {bot_id} not in g_bots: {list(g_bots.keys())}"}

    bot     = g_bots[bot_id]
    context = bot["context"]
    page    = await context.new_page()
    log     = []
    try:
        # Verify we're using the right account
        await page.goto("https://www.roblox.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        logged_in_as = await page.evaluate("() => window.Roblox?.CurrentUser?.Name ?? 'unknown'")
        log.append(f"bot={bot_id} logged_in_as={logged_in_as}")
        print(f"[bot={bot_id}] logged in as: {logged_in_as}")

        trade_url = f"https://www.roblox.com/users/{other_user_id}/trade"
        await page.goto(trade_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        # Resolve lih for local (bot) side
        if local_target_id.strip().lower() == "lih":
            bot_user_id = g_bots[bot_id].get("user_id", "")
            if not bot_user_id:
                return {"status": "error", "message": "Bot user_id not set — restart trade_bot", "log": log}
            resolved = await fetch_lowest_item(page, bot_user_id, max_rap=max_rap)
            if not resolved:
                return {"status": "error", "message": "Bot has no available (non-held) items", "log": log}
            if resolved == "NO_BOT_ITEM":
                return {"status": "error", "message": f"No bot item available with RAP ≤ {max_rap} — deposit item value is too low", "log": log}
            log.append(f"local lih resolved to targetId={resolved} (max_rap={max_rap})")
            local_target_id = resolved

        # Resolve lih for other side
        if other_target_id.strip().lower() == "lih":
            resolved = await fetch_lowest_item(page, other_user_id)
            if not resolved:
                return {"status": "error", "message": "No available items on their side", "log": log}
            log.append(f"other lih resolved to targetId={resolved}")
            other_target_id = resolved

        # Click items
        async def click_by_target_id(target_id, label):
            result = await page.evaluate(f"""() => {{
                const span = document.querySelector('span[thumbnail-target-id="{target_id}"]');
                if (!span) return 'span_not_found';
                const card = span.closest('.item-card-thumb-container');
                if (!card) return 'card_not_found';
                card.dispatchEvent(new MouseEvent('click', {{bubbles:true, cancelable:true, view:window}}));
                return 'ok';
            }}""")
            log.append(f"{label}: {result}")
            return result

        await click_by_target_id(local_target_id, "your item")
        await page.wait_for_timeout(800)
        await click_by_target_id(other_target_id, "their item")
        await page.wait_for_timeout(800)

        # Make Offer
        await page.wait_for_selector('button[ng-click="sendTrade()"]', timeout=12000)
        await page.evaluate("""() => {
            const btn = document.querySelector('button[ng-click="sendTrade()"]');
            if (btn) btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
        }""")
        log.append("make_offer: clicked")

        # Send confirmation modal
        try:
            await page.wait_for_selector('#modal-action-button', timeout=8000)
            await page.evaluate("""() => {
                const btn = document.querySelector('#modal-action-button');
                if (btn) btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
            }""")
            log.append("send_modal: clicked")
        except Exception as e:
            log.append(f"send_modal: not found ({e})")

        # 2FA
        try:
            await page.wait_for_selector('#two-step-verification-code-input', timeout=10000)
            code = pyotp.TOTP(bot["secret_key"]).now()
            await page.fill('#two-step-verification-code-input', code)
            await page.wait_for_timeout(500)
            result = await page.evaluate("""() => {
                const btn = Array.from(document.querySelectorAll('button')).find(
                    b => b.getAttribute('aria-label') === 'Verify'
                );
                if (!btn) return 'not_found';
                btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
                return 'ok';
            }""")
            log.append(f"2fa: code={code} verify={result}")
        except Exception as e:
            log.append(f"2fa: not required ({e})")

        await page.wait_for_timeout(4000)
        return {"status": "ok", "bot": bot_id, "log": log}

    except Exception as e:
        return {"status": "error", "bot": bot_id, "message": str(e), "log": log}
    finally:
        await page.close()


# ── Extra HTTP handlers ──────────────────────────────────────

async def handle_tradable(request: web.Request):
    q       = request.rel_url.query
    bot_id  = q.get("botid", "1").strip()
    user_id = q.get("userid", "").strip()
    if not user_id:
        return web.json_response({"error": "userid required"}, status=400)
    if bot_id not in g_bots:
        return web.json_response({"error": f"Bot {bot_id} not found"}, status=404)
    context = g_bots[bot_id]["context"]
    page    = await context.new_page()
    try:
        data = await page.evaluate(f"""async () => {{
            const r = await fetch(
                'https://trades.roblox.com/v2/users/{user_id}/tradableitems?sortBy=CreationTime&cursor=&limit=50&sortOrder=Desc',
                {{ credentials: 'include', headers: {{ Accept: 'application/json' }} }}
            );
            return await r.json();
        }}""")
        return web.json_response(data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    finally:
        await page.close()


async def handle_bot_info(request: web.Request):
    bot_id = request.rel_url.query.get("botid", "").strip()
    if bot_id not in g_bots:
        return web.json_response({"error": f"Bot {bot_id} not found"}, status=404)
    b = g_bots[bot_id]
    return web.json_response({"user_id": b.get("user_id", ""), "username": b.get("username", "")})


async def handle_bots(request: web.Request):
    return web.json_response({"bots": list(g_bots.keys())})


async def handle_check_trade(request: web.Request):
    """Fetch trade status through the browser (uses all session cookies)."""
    q        = request.rel_url.query
    trade_id = q.get("tradeid", "").strip()
    bot_id   = q.get("botid", "1").strip()
    if not trade_id:
        return web.json_response({"error": "tradeid required"}, status=400)
    # Use the first available bot if specified one is missing
    if bot_id not in g_bots:
        if not g_bots:
            return web.json_response({"error": "No bots available"}, status=503)
        bot_id = next(iter(g_bots))
    context = g_bots[bot_id]["context"]
    page    = await context.new_page()
    try:
        data = await page.evaluate(f"""async () => {{
            const r = await fetch(
                'https://trades.roblox.com/v1/trades/{trade_id}',
                {{ credentials: 'include', headers: {{ Accept: 'application/json' }} }}
            );
            if (!r.ok) return {{ _error: r.status, _text: await r.text() }};
            return await r.json();
        }}""")
        if data.get("_error"):
            return web.json_response(
                {"error": f"Roblox API {data['_error']}: {data.get('_text','')}"},
                status=data["_error"]
            )
        return web.json_response(data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    finally:
        await page.close()


# ── HTTP handler ─────────────────────────────────────────────

async def handle_trade(request: web.Request):
    q               = request.rel_url.query
    bot_id          = q.get("botid", "").strip()
    other_user_id   = q.get("othersideuserid", "").strip()
    local_target_id = q.get("targetid", "").strip()
    other_target_id = q.get("othersidetargetid", "").strip()
    max_rap         = int(q.get("max_rap", "0") or "0")

    if not all([bot_id, other_user_id, local_target_id, other_target_id]):
        return web.json_response(
            {"error": "Missing params. Need: botid, othersideuserid, targetid, othersidetargetid"},
            status=400
        )
    if bot_id not in g_bots:
        return web.json_response(
            {"error": f"Bot '{bot_id}' not found. Available: {list(g_bots.keys())}"},
            status=404
        )

    print(f"\n[>] bot={bot_id} other={other_user_id} local_tid={local_target_id} other_tid={other_target_id} max_rap={max_rap}")
    result = await do_trade(bot_id, other_user_id, local_target_id, other_target_id, max_rap=max_rap)
    print(f"[<] {result}")
    return web.json_response(result)


# ── Main ─────────────────────────────────────────────────────

async def main():
    config = load_config()

    # --setup <id>  →  configure a single bot then exit
    if "--setup" in sys.argv:
        idx = sys.argv.index("--setup")
        bot_id = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else input("Bot ID to set up: ").strip()
        setup_bot(config, bot_id)
        return

    if not config.get("bots"):
        print("No bots configured. Run:  python trade_bot.py --setup 1")
        return

    async with async_playwright() as p:
        headless = True
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--disable-notifications",
            "--disable-default-apps",
            "--mute-audio",
            "--no-first-run",
        ]

        # Launch a browser + context for every configured bot
        for bot_id, bot_cfg in config["bots"].items():
            browser = await p.chromium.launch(headless=headless, args=launch_args)
            context = await browser.new_context()

            await context.add_cookies([
                {"name": ".ROBLOSECURITY", "value": bot_cfg["roblosecurity"],
                 "domain": ".roblox.com", "path": "/", "secure": True, "sameSite": "None"},
                {"name": ".rbxsession",    "value": bot_cfg["rbxsession"],
                 "domain": ".roblox.com", "path": "/", "secure": True, "sameSite": "None"},
            ])

            # Quick login check + grab user ID via API (reliable)
            page = await context.new_page()
            await page.goto("https://users.roblox.com/v1/users/authenticated", wait_until="domcontentloaded")
            try:
                data     = await page.evaluate("() => JSON.parse(document.body.innerText)")
                user_id  = str(data.get("id", ""))
                username = data.get("name", "")
            except Exception:
                user_id  = ""
                username = ""
            found = bool(user_id)
            print(f"[{'+'if found else '!'}] Bot {bot_id}: user={username}({user_id})")
            await page.close()

            g_bots[bot_id] = {
                "context":    context,
                "secret_key": bot_cfg["secret_key"],
                "user_id":    user_id,
                "username":   username,
            }

            # TOTP printer thread per bot
            threading.Thread(
                target=totp_printer, args=(bot_id, bot_cfg["secret_key"]), daemon=True
            ).start()

        # HTTP server
        app = web.Application()
        app.router.add_get("/trade",        handle_trade)
        app.router.add_get("/tradable",     handle_tradable)
        app.router.add_get("/bot_info",     handle_bot_info)
        app.router.add_get("/bots",         handle_bots)
        app.router.add_get("/check_trade",  handle_check_trade)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 5000)
        await site.start()

        print(f"\n[*] Server on http://localhost:5000  —  bots: {list(g_bots.keys())}")
        print("[*] GET /trade?botid=1&othersideuserid=ID&targetid=TID&othersidetargetid=OTID|lih\n")

        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
