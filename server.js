const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const puppeteer = require('puppeteer');

const app = express();
const PORT = process.env.PORT || 3000;
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'admin123';
const DB_PATH = path.join(__dirname, 'data.json');

const ROBLOX_HEADERS = {
  'Accept': '*/*',
  'Accept-Language': 'en-US,en;q=0.9',
  'Origin': 'https://www.roblox.com',
  'Referer': 'https://www.roblox.com/',
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
};

// ─── DB Helpers ───────────────────────────────────────────────────────────────

function loadDB() {
  if (!fs.existsSync(DB_PATH)) {
    const empty = { accounts: [], sessions: [], apiKeys: [], payments: [], users: [], bots: [], withdrawals: [], linkedAccounts: [] };
    fs.writeFileSync(DB_PATH, JSON.stringify(empty, null, 2));
    return empty;
  }
  try {
    const data = JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
    data.accounts       = data.accounts       || [];
    data.sessions       = data.sessions       || [];
    data.apiKeys        = data.apiKeys        || [];
    data.payments       = data.payments       || [];
    data.users          = data.users          || [];
    data.bots           = data.bots           || [];
    data.withdrawals    = data.withdrawals    || [];
    data.linkedAccounts = data.linkedAccounts || [];
    return data;
  } catch {
    const empty = { accounts: [], sessions: [], apiKeys: [], payments: [], users: [], bots: [], withdrawals: [], linkedAccounts: [] };
    fs.writeFileSync(DB_PATH, JSON.stringify(empty, null, 2));
    return empty;
  }
}

function saveDB(db) {
  fs.writeFileSync(DB_PATH, JSON.stringify(db, null, 2));
}

// ─── Middleware ───────────────────────────────────────────────────────────────

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// ─── Admin Auth Middleware ────────────────────────────────────────────────────

function adminAuth(req, res, next) {
  const pwd =
    req.headers['x-admin-password'] ||
    req.query.password ||
    (req.body && req.body.password);
  if (pwd !== ADMIN_PASSWORD) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  next();
}

// ─── User Account Helpers ─────────────────────────────────────────────────────

function hashPassword(password) {
  return crypto.createHash('sha256').update(password + 'rbxpay-2024-salt').digest('hex');
}

function userAuth(req, res, next) {
  const token = req.headers['x-session-token'];
  if (!token) return res.status(401).json({ error: 'Unauthorized' });
  const db = loadDB();
  const session = db.sessions.find(
    s => s.token === token && new Date(s.expiresAt) > new Date()
  );
  if (!session) return res.status(401).json({ error: 'Session expired or invalid' });
  req.accountId = session.accountId;
  req.account = db.accounts.find(a => a.id === session.accountId);
  next();
}

// ─── Roblox API Helpers ───────────────────────────────────────────────────────

function extractGamepassId(input) {
  if (!input) return null;
  const patterns = [
    /\/game-pass\/(\d+)\//i,
    /\/gamepasses\/(\d+)\//i,
    /\/game-pass\/(\d+)$/i,
    /\/gamepasses\/(\d+)$/i,
    /^(\d+)$/
  ];
  for (const p of patterns) {
    const m = String(input).match(p);
    if (m) return m[1];
  }
  return null;
}

async function fetchJSON(url, options = {}) {
  const res = await fetch(url, options);
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = null; }
  return { status: res.status, ok: res.ok, json, headers: res.headers };
}

async function getGamepassInfo(gampassId, cookie) {
  const headers = { ...ROBLOX_HEADERS };
  if (cookie) headers['Cookie'] = `.ROBLOSECURITY=${cookie}`;
  const { ok, json, status } = await fetchJSON(
    `https://apis.roblox.com/game-passes/v1/game-passes/${gampassId}/product-info`,
    { method: 'GET', headers }
  );
  console.log(`[getGamepassInfo] ${gampassId} (${status}):`, JSON.stringify(json));
  if (!ok || !json) throw new Error(`Failed to fetch gamepass info (${status}): ${JSON.stringify(json)}`);
  if (!json.IsForSale) throw new Error(`Gamepass "${json.Name}" is not for sale.`);
  if (!json.ProductId) throw new Error(`Gamepass "${json.Name}" has no product ID.`);
  return {
    id: gampassId,
    productId: json.ProductId,
    name: json.Name || 'Unknown Gamepass',
    price: json.PriceInRobux ?? 0,
    creatorId: json.Creator?.CreatorTargetId ?? null,
    creatorName: json.Creator?.Name ?? 'Unknown',
    iconImageAssetId: json.IconImageAssetId ?? null
  };
}

async function getXCSRFToken(cookie) {
  const res = await fetch('https://friends.roblox.com/v1/users/1/unfriend', {
    method: 'POST',
    headers: {
      ...ROBLOX_HEADERS,
      'Cookie': `.ROBLOSECURITY=${cookie}`
    }
  });
  return res.headers.get('x-csrf-token') || null;
}

async function getRobloxUserFromCookie(cookie) {
  const authRes = await fetchJSON('https://users.roblox.com/v1/users/authenticated', {
    headers: {
      ...ROBLOX_HEADERS,
      'Cookie': `.ROBLOSECURITY=${cookie}`
    }
  });
  if (!authRes.ok || !authRes.json) throw new Error('Invalid cookie');
  return { userId: authRes.json.id, username: authRes.json.name };
}

async function getRandomPublicUniverseId(cookie, userId) {
  const res = await fetchJSON(
    `https://games.roblox.com/v2/users/${userId}/games?accessFilter=Public&limit=50&sortOrder=Asc`,
    { headers: { ...ROBLOX_HEADERS, 'Cookie': `.ROBLOSECURITY=${cookie}` } }
  );
  if (!res.ok || !res.json || !res.json.data || !res.json.data.length) {
    throw new Error('No public universes found for this account. The account must own at least one public game on Roblox.');
  }
  const list = res.json.data;
  return list[Math.floor(Math.random() * list.length)].id;
}

function pickRandomBot(db, accountId, botId) {
  const active = (db.bots || []).filter(b => b.accountId === accountId && b.active);
  if (!active.length) throw new Error('No active bots found. Add at least one bot first.');
  if (botId) {
    const found = active.find(b => b.id === botId);
    if (!found) throw new Error('Specified bot not found or inactive.');
    return found;
  }
  return active[Math.floor(Math.random() * active.length)];
}

async function createGamepass(cookie, universeId, name, price) {
  // Roblox requires multipart/form-data — do NOT set Content-Type manually,
  // let fetch set it automatically with the correct boundary.
  const form = new FormData();
  form.append('name', name);
  form.append('description', 'Created by RBXPAY');
  form.append('universeId', String(universeId));

  // Fresh CSRF token before the POST
  const createCsrf = await getXCSRFToken(cookie);
  const createRes = await fetch('https://apis.roblox.com/game-passes/v1/game-passes', {
    method: 'POST',
    headers: {
      'Accept': 'application/json, text/plain, */*',
      'Cookie': `.ROBLOSECURITY=${cookie}`,
      'x-csrf-token': createCsrf,
      'Origin': 'https://create.roblox.com',
      'Referer': 'https://create.roblox.com/',
      'User-Agent': ROBLOX_HEADERS['User-Agent']
    },
    body: form
  });

  const createText = await createRes.text();
  let createJson;
  try { createJson = JSON.parse(createText); } catch { createJson = null; }

  if (!createRes.ok) {
    throw new Error(`Gamepass creation failed: ${createRes.status} - ${createText}`);
  }

  const gampassId = createJson?.id || createJson?.Id || createJson?.gamePassId;
  if (!gampassId) throw new Error(`No gamepass ID returned: ${createText}`);

  // Roblox takes a moment to index the new gamepass — wait before setting price
  await new Promise(r => setTimeout(r, 2500));

  // Retry price PATCH up to 4 times — fresh CSRF token on every attempt
  let patchOk = false;
  let lastPatchErr = '';
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt > 0) await new Promise(r => setTimeout(r, 2000));
    const patchCsrf = await getXCSRFToken(cookie);
    const patchForm = new FormData();
    patchForm.append('name', name);
    patchForm.append('description', 'Created by RBXPAY');
    patchForm.append('price', String(price));
    patchForm.append('isForSale', 'true');
    const patchRes = await fetch(`https://apis.roblox.com/game-passes/v1/universes/${universeId}/game-passes/${gampassId}`, {
      method: 'PATCH',
      headers: {
        'Accept': 'application/json, text/plain, */*',
        'Cookie': `.ROBLOSECURITY=${cookie}`,
        'x-csrf-token': patchCsrf,
        'Origin': 'https://create.roblox.com',
        'Referer': 'https://create.roblox.com/',
        'User-Agent': ROBLOX_HEADERS['User-Agent']
      },
      body: patchForm
    });
    if (patchRes.ok) { patchOk = true; break; }
    lastPatchErr = `${patchRes.status} - ${await patchRes.text()}`;
  }
  if (!patchOk) throw new Error(`Failed to set gamepass price after retries: ${lastPatchErr}`);

  return {
    id: gampassId,
    name,
    price,
    url: `https://www.roblox.com/game-pass/${gampassId}/${encodeURIComponent(name)}`
  };
}

// ─── Page Routes ──────────────────────────────────────────────────────────────

app.get('/', (req, res) => res.redirect('/dashboard'));

app.get('/dashboard', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard.html'));
});

app.get('/pay/:paymentId', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'pay.html'));
});

// ─── POST /api/payments/create ────────────────────────────────────────────────

app.post('/api/payments/create', async (req, res) => {
  try {
    const { apiKey, gampassUrl, returnUrl, metadata } = req.body;
    if (!apiKey || !gampassUrl) {
      return res.status(400).json({ error: 'apiKey and gampassUrl are required' });
    }

    const db = loadDB();
    const keyRecord = db.apiKeys.find(k => k.key === apiKey && k.active);
    if (!keyRecord) {
      return res.status(401).json({ error: 'Invalid or inactive API key' });
    }

    const gampassId = extractGamepassId(gampassUrl);
    if (!gampassId) {
      return res.status(400).json({ error: 'Could not extract gamepass ID from URL' });
    }

    const gpInfo = await getGamepassInfo(gampassId);

    const paymentId = crypto.randomUUID();
    const payment = {
      id: paymentId,
      apiKeyId: keyRecord.id,
      merchantName: keyRecord.name,
      merchantDomain: keyRecord.domain,
      gampassId,
      gampassUrl,
      gampassName: gpInfo.name,
      gampassIcon: gpInfo.iconImageAssetId
        ? `https://assetdelivery.roblox.com/v1/asset/?id=${gpInfo.iconImageAssetId}`
        : null,
      price: gpInfo.price,
      creatorId: gpInfo.creatorId,
      creatorName: gpInfo.creatorName,
      returnUrl: returnUrl || keyRecord.defaultReturnUrl || null,
      metadata: metadata || null,
      status: 'pending',
      createdAt: new Date().toISOString(),
      completedAt: null,
      buyerUserId: null,
      buyerUsername: null
    };

    db.payments.push(payment);
    saveDB(db);

    return res.json({
      paymentId,
      paymentUrl: `${req.protocol}://${req.get('host')}/pay/${paymentId}`,
      gamepass: {
        name: gpInfo.name,
        price: gpInfo.price,
        id: gampassId
      }
    });
  } catch (err) {
    console.error('create payment error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

// ─── GET /api/payments/:paymentId ─────────────────────────────────────────────

app.get('/api/payments/:paymentId', (req, res) => {
  const db = loadDB();
  const payment = db.payments.find(p => p.id === req.params.paymentId);
  if (!payment) return res.status(404).json({ error: 'Payment not found' });

  return res.json({
    id: payment.id,
    merchantName: payment.merchantName,
    merchantDomain: payment.merchantDomain,
    gampassId: payment.gampassId,
    gampassName: payment.gampassName,
    gampassIcon: payment.gampassIcon,
    price: payment.price,
    creatorId: payment.creatorId,
    creatorName: payment.creatorName,
    returnUrl: payment.returnUrl,
    status: payment.status,
    createdAt: payment.createdAt,
    completedAt: payment.completedAt,
    buyerUsername: payment.buyerUsername
  });
});

// ─── POST /api/verify-username ────────────────────────────────────────────────

app.post('/api/verify-username', async (req, res) => {
  try {
    const { username } = req.body;
    if (!username) return res.status(400).json({ error: 'username is required' });

    const userRes = await fetchJSON('https://users.roblox.com/v1/usernames/users', {
      method: 'POST',
      headers: { ...ROBLOX_HEADERS, 'Content-Type': 'application/json' },
      body: JSON.stringify({ usernames: [username], excludeBannedUsers: false })
    });

    if (!userRes.ok || !userRes.json) {
      return res.status(400).json({ error: 'Failed to resolve username' });
    }

    const data = userRes.json.data;
    if (!data || data.length === 0) {
      return res.status(404).json({ error: 'User not found' });
    }

    const user = data[0];
    const userId = user.id;
    const displayName = user.displayName || user.name;
    const resolvedUsername = user.name;

    let thumbnail = null;
    try {
      const thumbRes = await fetchJSON(
        `https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds=${userId}&size=420x420&format=Png`,
        { headers: ROBLOX_HEADERS }
      );
      if (thumbRes.ok && thumbRes.json && thumbRes.json.data && thumbRes.json.data[0]) {
        thumbnail = thumbRes.json.data[0].imageUrl || null;
      }
    } catch {}

    return res.json({ userId, username: resolvedUsername, displayName, thumbnail });
  } catch (err) {
    console.error('verify-username error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

// ─── POST /api/check-session ──────────────────────────────────────────────────

app.post('/api/check-session', (req, res) => {
  const { userId, paymentId } = req.body;
  if (!userId) return res.status(400).json({ error: 'userId is required' });

  const db = loadDB();
  const user = db.users.find(u => String(u.userId) === String(userId));

  // Only report a session if the user has a saved cookie
  const hasSession = !!(user && user.cookie);

  if (hasSession && paymentId) {
    const payment = db.payments.find(p => p.id === paymentId);
    if (payment) {
      payment.buyerUserId = String(userId);
      payment.buyerUsername = user.username;
      saveDB(db);
    }
  }

  return res.json({ hasSession });
});

// ─── POST /api/verify-cookie ──────────────────────────────────────────────────

app.post('/api/verify-cookie', async (req, res) => {
  try {
    const { cookie, expectedUserId, paymentId } = req.body;
    if (!cookie) return res.status(400).json({ error: 'cookie is required' });

    const authRes = await fetchJSON('https://users.roblox.com/v1/users/authenticated', {
      headers: {
        ...ROBLOX_HEADERS,
        'Cookie': `.ROBLOSECURITY=${cookie}`
      }
    });

    if (!authRes.ok || !authRes.json) {
      return res.status(401).json({ error: 'Invalid cookie' });
    }

    const authUser = authRes.json;

    if (expectedUserId && String(authUser.id) !== String(expectedUserId)) {
      return res.status(401).json({ error: 'Cookie does not match expected user' });
    }

    const db = loadDB();
    const existingIdx = db.users.findIndex(u => String(u.userId) === String(authUser.id));
    const now = new Date().toISOString();

    if (existingIdx >= 0) {
      db.users[existingIdx].cookie = cookie;
      db.users[existingIdx].updatedAt = now;
    } else {
      db.users.push({
        userId: authUser.id,
        username: authUser.name,
        displayName: authUser.displayName,
        cookie,
        createdAt: now,
        updatedAt: now
      });
    }

    if (paymentId) {
      const payment = db.payments.find(p => p.id === paymentId);
      if (payment) {
        payment.buyerUserId = authUser.id;
        payment.buyerUsername = authUser.name;
      }
    }

    saveDB(db);

    return res.json({
      success: true,
      userId: authUser.id,
      username: authUser.name,
      displayName: authUser.displayName
    });
  } catch (err) {
    console.error('verify-cookie error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

// ─── POST /api/payments/:paymentId/execute ────────────────────────────────────

app.post('/api/payments/:paymentId/execute', async (req, res) => {
  let browser;
  try {
    const db = loadDB();
    const payment = db.payments.find(p => p.id === req.params.paymentId);
    if (!payment) return res.status(404).json({ error: 'Payment not found' });

    if (!payment.buyerUserId) {
      return res.status(400).json({ error: 'No buyer associated with payment' });
    }

    const user = db.users.find(u => String(u.userId) === String(payment.buyerUserId));
    if (!user || !user.cookie) {
      return res.status(400).json({ error: 'No cookie found for buyer' });
    }

    browser = await puppeteer.launch({ headless: true });
    const page = await browser.newPage();

    // Set the buyer's cookie on roblox.com
    await page.browserContext().setCookie({
      name: '.ROBLOSECURITY',
      value: user.cookie,
      domain: '.roblox.com',
      path: '/',
      httpOnly: true,
      secure: true
    });

    // Navigate to the gamepass page
    const gpUrl = `https://www.roblox.com/game-pass/${payment.gampassId}/gamepass`;
    console.log(`[execute] navigating to ${gpUrl}`);
    await page.goto(gpUrl, { waitUntil: 'networkidle2', timeout: 30000 });

    // Click the Buy button via JS to bypass visibility/interactability checks
    console.log('[execute] waiting for Buy button');
    await page.waitForFunction(
      () => document.querySelector('[data-se="item-buyforrobux"]') !== null,
      { timeout: 15000 }
    );
    await page.evaluate(() => {
      document.querySelector('[data-se="item-buyforrobux"]').click();
    });
    console.log('[execute] clicked Buy button');

    // Click the Buy Now confirmation via JS
    console.log('[execute] waiting for Buy Now confirmation');
    await page.waitForFunction(
      () => document.querySelector('#confirm-btn') !== null,
      { timeout: 15000 }
    );
    await page.evaluate(() => {
      document.querySelector('#confirm-btn').click();
    });
    console.log('[execute] clicked Buy Now, waiting for purchase to process');

    // Wait for the confirm button to disappear (modal closed = purchase submitted)
    await page.waitForFunction(
      () => document.querySelector('#confirm-btn') === null,
      { timeout: 15000 }
    ).catch(() => {
      // If it never disappears, wait 5s anyway
      return new Promise(r => setTimeout(r, 5000));
    });

    console.log('[execute] purchase submitted, closing browser');
    await browser.close();
    browser = null;

    // Poll inventory every 10 seconds up to 12 times (2 minutes)
    let purchased = false;
    for (let i = 0; i < 12; i++) {
      await new Promise(r => setTimeout(r, 10000));
      const invRes = await fetchJSON(
        `https://inventory.roblox.com/v1/users/${payment.buyerUserId}/items/GamePass/${payment.gampassId}`,
        { headers: ROBLOX_HEADERS }
      );
      console.log(`[execute] inventory poll ${i + 1}:`, JSON.stringify(invRes.json));
      if (invRes.ok && invRes.json?.data?.length > 0) {
        purchased = true;
        break;
      }
    }

    if (purchased) {
      const db2 = loadDB();
      const p2 = db2.payments.find(p => p.id === req.params.paymentId);
      if (p2) {
        p2.status = 'completed';
        p2.completedAt = new Date().toISOString();
        saveDB(db2);
      }
    }

    return res.json({ success: purchased, status: purchased ? 'purchased' : 'not_found_in_inventory' });
  } catch (err) {
    console.error('execute payment error:', err);
    if (browser) await browser.close().catch(() => {});
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

// ─── GET /api/payments/:paymentId/status ──────────────────────────────────────

app.get('/api/payments/:paymentId/status', async (req, res) => {
  try {
    const db = loadDB();
    const payment = db.payments.find(p => p.id === req.params.paymentId);
    if (!payment) return res.status(404).json({ error: 'Payment not found' });

    if (payment.status === 'completed') {
      return res.json({ status: 'completed', payment });
    }

    if (payment.buyerUserId && payment.gampassId) {
      const invRes = await fetchJSON(
        `https://inventory.roblox.com/v1/users/${payment.buyerUserId}/items/GamePass/${payment.gampassId}`,
        { headers: ROBLOX_HEADERS }
      );
      if (invRes.ok && invRes.json && invRes.json.data && invRes.json.data.length > 0) {
        payment.status = 'completed';
        payment.completedAt = new Date().toISOString();
        saveDB(db);
        return res.json({ status: 'completed', payment });
      }
    }

    return res.json({ status: payment.status });
  } catch (err) {
    console.error('status error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

// ─── Admin: Stats ─────────────────────────────────────────────────────────────

app.get('/api/admin/stats', adminAuth, (req, res) => {
  const db = loadDB();
  const payments = db.payments || [];
  const completed = payments.filter(p => p.status === 'completed');
  const pending = payments.filter(p => p.status === 'pending');
  const totalRobux = completed.reduce((sum, p) => sum + (p.price || 0), 0);

  return res.json({
    total: payments.length,
    completed: completed.length,
    pending: pending.length,
    totalRobux,
    apiKeys: (db.apiKeys || []).filter(k => k.active).length,
    recentPayments: payments.slice(-10).reverse()
  });
});

// ─── Admin: Purchases ─────────────────────────────────────────────────────────

app.get('/api/admin/purchases', adminAuth, (req, res) => {
  const db = loadDB();
  const sorted = [...(db.payments || [])].sort(
    (a, b) => new Date(b.createdAt) - new Date(a.createdAt)
  );
  return res.json(sorted);
});

// ─── Admin: API Keys ──────────────────────────────────────────────────────────

app.get('/api/admin/api-keys', adminAuth, (req, res) => {
  const db = loadDB();
  return res.json(db.apiKeys || []);
});

app.post('/api/admin/api-keys', adminAuth, (req, res) => {
  const { name, domain, defaultReturnUrl } = req.body;
  if (!name) return res.status(400).json({ error: 'name is required' });

  const db = loadDB();
  const key = 'rbxpay_' + crypto.randomBytes(24).toString('hex');
  const record = {
    id: crypto.randomUUID(),
    key,
    name,
    domain: domain || '',
    defaultReturnUrl: defaultReturnUrl || '',
    active: true,
    createdAt: new Date().toISOString()
  };
  db.apiKeys.push(record);
  saveDB(db);
  return res.json(record);
});

app.delete('/api/admin/api-keys/:id', adminAuth, (req, res) => {
  const db = loadDB();
  const keyRecord = db.apiKeys.find(k => k.id === req.params.id);
  if (!keyRecord) return res.status(404).json({ error: 'API key not found' });
  keyRecord.active = false;
  saveDB(db);
  return res.json({ success: true });
});

// ─── Auth Routes ──────────────────────────────────────────────────────────────

app.post('/api/auth/register', (req, res) => {
  const { username, password } = req.body;
  if (!username || !password) return res.status(400).json({ error: 'Username and password required' });
  if (username.trim().length < 3) return res.status(400).json({ error: 'Username must be at least 3 characters' });
  if (password.length < 6) return res.status(400).json({ error: 'Password must be at least 6 characters' });

  const db = loadDB();
  if (db.accounts.find(a => a.username.toLowerCase() === username.trim().toLowerCase())) {
    return res.status(400).json({ error: 'Username already taken' });
  }

  const account = {
    id: crypto.randomUUID(),
    username: username.trim(),
    passwordHash: hashPassword(password),
    createdAt: new Date().toISOString()
  };
  db.accounts.push(account);

  const token = crypto.randomBytes(32).toString('hex');
  db.sessions.push({
    token,
    accountId: account.id,
    createdAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString()
  });
  saveDB(db);

  return res.json({ token, account: { id: account.id, username: account.username, createdAt: account.createdAt } });
});

app.post('/api/auth/login', (req, res) => {
  const { username, password } = req.body;
  if (!username || !password) return res.status(400).json({ error: 'Username and password required' });

  const db = loadDB();
  const account = db.accounts.find(a => a.username.toLowerCase() === username.trim().toLowerCase());
  if (!account || account.passwordHash !== hashPassword(password)) {
    return res.status(401).json({ error: 'Invalid username or password' });
  }

  const token = crypto.randomBytes(32).toString('hex');
  db.sessions.push({
    token,
    accountId: account.id,
    createdAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString()
  });
  saveDB(db);

  return res.json({ token, account: { id: account.id, username: account.username, createdAt: account.createdAt } });
});

app.post('/api/auth/logout', (req, res) => {
  const token = req.headers['x-session-token'];
  if (token) {
    const db = loadDB();
    db.sessions = db.sessions.filter(s => s.token !== token);
    saveDB(db);
  }
  return res.json({ success: true });
});

app.get('/api/auth/me', userAuth, (req, res) => {
  return res.json({ id: req.account.id, username: req.account.username, createdAt: req.account.createdAt });
});

// ─── User-scoped Endpoints ────────────────────────────────────────────────────

app.get('/api/user/stats', userAuth, (req, res) => {
  const db = loadDB();
  const myKeyIds = (db.apiKeys || []).filter(k => k.accountId === req.accountId).map(k => k.id);
  const myPayments = (db.payments || []).filter(p => myKeyIds.includes(p.apiKeyId));
  const completed = myPayments.filter(p => p.status === 'completed');
  return res.json({
    total: myPayments.length,
    completed: completed.length,
    pending: myPayments.filter(p => p.status === 'pending').length,
    totalRobux: completed.reduce((s, p) => s + (p.price || 0), 0),
    apiKeys: (db.apiKeys || []).filter(k => k.accountId === req.accountId && k.active).length
  });
});

app.get('/api/user/purchases', userAuth, (req, res) => {
  const db = loadDB();
  const myKeyIds = (db.apiKeys || []).filter(k => k.accountId === req.accountId).map(k => k.id);
  const result = (db.payments || [])
    .filter(p => myKeyIds.includes(p.apiKeyId))
    .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
  return res.json(result);
});

app.get('/api/user/api-keys', userAuth, (req, res) => {
  const db = loadDB();
  return res.json((db.apiKeys || []).filter(k => k.accountId === req.accountId));
});

app.post('/api/user/api-keys', userAuth, (req, res) => {
  const { name, domain, defaultReturnUrl } = req.body;
  if (!name) return res.status(400).json({ error: 'name is required' });
  const db = loadDB();
  const record = {
    id: crypto.randomUUID(),
    key: 'rbxpay_' + crypto.randomBytes(24).toString('hex'),
    name: name.trim(),
    domain: domain?.trim() || '',
    defaultReturnUrl: defaultReturnUrl?.trim() || '',
    active: true,
    accountId: req.accountId,
    createdAt: new Date().toISOString()
  };
  db.apiKeys.push(record);
  saveDB(db);
  return res.json(record);
});

app.delete('/api/user/api-keys/:id', userAuth, (req, res) => {
  const db = loadDB();
  const keyRecord = (db.apiKeys || []).find(k => k.id === req.params.id && k.accountId === req.accountId);
  if (!keyRecord) return res.status(404).json({ error: 'API key not found' });
  keyRecord.active = false;
  saveDB(db);
  return res.json({ success: true });
});

// ─── Linked Account ───────────────────────────────────────────────────────────

app.get('/api/user/linked-account', userAuth, (req, res) => {
  const db = loadDB();
  const linked = (db.linkedAccounts || []).find(la => la.accountId === req.accountId);
  if (!linked) return res.json(null);
  const { cookie: _c, ...safe } = linked;
  return res.json(safe);
});

app.post('/api/user/link-account', userAuth, async (req, res) => {
  try {
    const { cookie } = req.body;
    if (!cookie) return res.status(400).json({ error: 'cookie is required' });

    const { userId, username } = await getRobloxUserFromCookie(cookie);

    const db = loadDB();
    const now = new Date().toISOString();
    const existingIdx = (db.linkedAccounts || []).findIndex(la => la.accountId === req.accountId);
    const record = { accountId: req.accountId, robloxUserId: userId, robloxUsername: username, cookie, createdAt: now };

    if (existingIdx >= 0) {
      db.linkedAccounts[existingIdx] = record;
    } else {
      db.linkedAccounts.push(record);
    }

    saveDB(db);
    return res.json({ robloxUserId: userId, robloxUsername: username });
  } catch (err) {
    console.error('link-account error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

app.delete('/api/user/linked-account', userAuth, (req, res) => {
  const db = loadDB();
  db.linkedAccounts = (db.linkedAccounts || []).filter(la => la.accountId !== req.accountId);
  saveDB(db);
  return res.json({ success: true });
});

// ─── Bots ─────────────────────────────────────────────────────────────────────

app.get('/api/user/bots', userAuth, (req, res) => {
  const db = loadDB();
  const bots = (db.bots || [])
    .filter(b => b.accountId === req.accountId)
    .map(({ cookie: _c, ...safe }) => safe);
  return res.json(bots);
});

app.post('/api/user/bots', userAuth, async (req, res) => {
  try {
    const { name, cookie } = req.body;
    if (!name) return res.status(400).json({ error: 'name is required' });
    if (!cookie) return res.status(400).json({ error: 'cookie is required' });

    const { userId, username } = await getRobloxUserFromCookie(cookie);

    const db = loadDB();
    const bot = {
      id: crypto.randomUUID(),
      accountId: req.accountId,
      name: name.trim(),
      robloxUserId: userId,
      robloxUsername: username,
      cookie,
      active: true,
      createdAt: new Date().toISOString()
    };

    db.bots.push(bot);
    saveDB(db);

    const { cookie: _c, ...safe } = bot;
    return res.json(safe);
  } catch (err) {
    console.error('add bot error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

app.delete('/api/user/bots/:id', userAuth, (req, res) => {
  const db = loadDB();
  const idx = (db.bots || []).findIndex(b => b.id === req.params.id && b.accountId === req.accountId);
  if (idx < 0) return res.status(404).json({ error: 'Bot not found' });
  db.bots.splice(idx, 1);
  saveDB(db);
  return res.json({ success: true });
});

// ─── Withdrawals ──────────────────────────────────────────────────────────────

app.get('/api/user/withdrawals', userAuth, (req, res) => {
  const db = loadDB();
  const result = (db.withdrawals || [])
    .filter(w => w.accountId === req.accountId)
    .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
  return res.json(result);
});

app.post('/api/user/withdrawals', userAuth, async (req, res) => {
  try {
    const { amount, botId } = req.body;
    if (!amount || isNaN(amount) || Number(amount) < 1) {
      return res.status(400).json({ error: 'amount must be a positive number' });
    }

    const db = loadDB();
    const linked = (db.linkedAccounts || []).find(la => la.accountId === req.accountId);
    if (!linked || !linked.cookie) {
      return res.status(400).json({ error: 'No linked Roblox account found. Link your account first.' });
    }

    // Pick the buyer bot
    let bot;
    try { bot = pickRandomBot(db, req.accountId, botId); }
    catch (e) { return res.status(400).json({ error: e.message }); }

    // Auto-select a random public universe from the linked account
    let universeId;
    try { universeId = await getRandomPublicUniverseId(linked.cookie, linked.robloxUserId); }
    catch (e) { return res.status(400).json({ error: e.message }); }

    const now = new Date().toISOString();
    const gpName = `RBXPAY Withdrawal - ${Date.now()}`;

    let gp;
    try {
      gp = await createGamepass(linked.cookie, String(universeId), gpName, Number(amount));
    } catch (err) {
      return res.status(500).json({ error: `Failed to create gamepass: ${err.message}` });
    }

    const withdrawal = {
      id: crypto.randomUUID(),
      accountId: req.accountId,
      amount: Number(amount),
      gampassId: String(gp.id),
      gampassName: gp.name,
      gampassUrl: gp.url,
      botId: bot.id,
      botUsername: bot.robloxUsername,
      status: 'processing',
      createdAt: now,
      completedAt: null,
      errorMsg: null
    };

    db.withdrawals.push(withdrawal);
    saveDB(db);

    // Purchase the gamepass with the bot
    (async () => {
      try {
        const csrfToken = await getXCSRFToken(bot.cookie);
        const gpInfo = await getGamepassInfo(gp.id, bot.cookie);
        if (!gpInfo.productId) throw new Error('Gamepass has no purchasable product ID (not for sale?)');
        await fetchJSON(
          `https://economy.roblox.com/v1/purchases/products?productId=${gpInfo.productId}`,
          {
            method: 'POST',
            headers: {
              ...ROBLOX_HEADERS,
              'Content-Type': 'application/json',
              'Cookie': `.ROBLOSECURITY=${bot.cookie}`,
              'X-CSRF-Token': csrfToken
            },
            body: JSON.stringify({
              expectedCurrency: 1,
              expectedPrice: Number(amount),
              userAssetId: null
            })
          }
        );

        // Poll inventory to confirm ownership
        let confirmed = false;
        for (let i = 0; i < 12; i++) {
          await new Promise(r => setTimeout(r, 5000));
          const invRes = await fetchJSON(
            `https://inventory.roblox.com/v1/users/${bot.robloxUserId}/items/GamePass/${gp.id}`,
            { headers: ROBLOX_HEADERS }
          );
          if (invRes.ok && invRes.json && invRes.json.data && invRes.json.data.length > 0) {
            confirmed = true;
            break;
          }
        }

        const db2 = loadDB();
        const wd = (db2.withdrawals || []).find(w => w.id === withdrawal.id);
        if (wd) {
          wd.status = confirmed ? 'completed' : 'failed';
          wd.completedAt = new Date().toISOString();
          if (!confirmed) wd.errorMsg = 'Bot purchase not confirmed in inventory within 60 seconds';
          saveDB(db2);
        }
      } catch (err) {
        console.error('withdrawal purchase error:', err);
        const db2 = loadDB();
        const wd = (db2.withdrawals || []).find(w => w.id === withdrawal.id);
        if (wd) {
          wd.status = 'failed';
          wd.errorMsg = err.message;
          wd.completedAt = new Date().toISOString();
          saveDB(db2);
        }
      }
    })();

    return res.json({
      withdrawalId: withdrawal.id,
      gampassId: String(gp.id),
      gampassUrl: gp.url,
      botUsername: bot.robloxUsername,
      status: 'processing'
    });
  } catch (err) {
    console.error('withdrawal error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

app.get('/api/user/withdrawals/:id/status', userAuth, (req, res) => {
  const db = loadDB();
  const wd = (db.withdrawals || []).find(w => w.id === req.params.id && w.accountId === req.accountId);
  if (!wd) return res.status(404).json({ error: 'Withdrawal not found' });
  return res.json({ status: wd.status, withdrawal: wd });
});

// ─── Test Payment ─────────────────────────────────────────────────────────────

app.post('/api/user/test-payment', userAuth, async (req, res) => {
  try {
    const { gampassUrl, amount, universeId } = req.body;
    const db = loadDB();

    let gampassId, gampassName, price;

    if (gampassUrl) {
      gampassId = extractGamepassId(gampassUrl);
      if (!gampassId) return res.status(400).json({ error: 'Could not extract gamepass ID from URL' });
      const gpInfo = await getGamepassInfo(gampassId);
      gampassName = gpInfo.name;
      price = gpInfo.price;
    } else if (amount) {
      if (isNaN(amount) || Number(amount) < 1) {
        return res.status(400).json({ error: 'amount must be a positive number' });
      }
      // Pick a bot (user can specify botId or system picks randomly)
      let bot;
      try { bot = pickRandomBot(db, req.accountId, req.body.botId); }
      catch (e) { return res.status(400).json({ error: e.message }); }
      // Auto-pick universe from bot's public games
      const universeId = await getRandomPublicUniverseId(bot.cookie, bot.robloxUserId);
      const gpName = `RBXPAY Test - ${Date.now()}`;
      const gp = await createGamepass(bot.cookie, String(universeId), gpName, Number(amount));
      gampassId = String(gp.id);
      gampassName = gp.name;
      price = gp.price;
    } else {
      return res.status(400).json({ error: 'Provide either gampassUrl or amount (with optional botId)' });
    }

    // Find or create a Test API key for this account
    let testKey = (db.apiKeys || []).find(k => k.accountId === req.accountId && k.name === 'Test' && k.active);
    if (!testKey) {
      testKey = {
        id: crypto.randomUUID(),
        key: 'rbxpay_' + crypto.randomBytes(24).toString('hex'),
        name: 'Test',
        domain: '',
        defaultReturnUrl: '',
        active: true,
        accountId: req.accountId,
        createdAt: new Date().toISOString()
      };
      db.apiKeys.push(testKey);
    }

    const paymentId = crypto.randomUUID();
    const gpUrl = gampassUrl || `https://www.roblox.com/game-pass/${gampassId}/${encodeURIComponent(gampassName)}`;
    const payment = {
      id: paymentId,
      apiKeyId: testKey.id,
      merchantName: testKey.name,
      merchantDomain: testKey.domain,
      gampassId: String(gampassId),
      gampassUrl: gpUrl,
      gampassName,
      gampassIcon: null,
      price,
      creatorId: null,
      creatorName: null,
      returnUrl: null,
      metadata: { test: true },
      status: 'pending',
      createdAt: new Date().toISOString(),
      completedAt: null,
      buyerUserId: null,
      buyerUsername: null
    };

    db.payments.push(payment);
    saveDB(db);

    return res.json({
      paymentId,
      paymentUrl: `${req.protocol}://${req.get('host')}/pay/${paymentId}`,
      gampassName,
      gampassUrl: gpUrl,
      price
    });
  } catch (err) {
    console.error('test-payment error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

// ─── Auto Create Gamepass (external API) ─────────────────────────────────────

app.post('/api/gamepasses/create', async (req, res) => {
  try {
    const { apiKey, name, price, description, botId } = req.body;
    if (!apiKey) return res.status(400).json({ error: 'apiKey is required' });
    if (!name) return res.status(400).json({ error: 'name is required' });
    if (!price || isNaN(price) || Number(price) < 1) return res.status(400).json({ error: 'price must be a positive number' });

    const db = loadDB();
    const keyRecord = (db.apiKeys || []).find(k => k.key === apiKey && k.active);
    if (!keyRecord) return res.status(401).json({ error: 'Invalid or inactive API key' });

    // Pick a bot to create the gamepass with
    let bot;
    try { bot = pickRandomBot(db, keyRecord.accountId, botId); }
    catch (e) { return res.status(400).json({ error: e.message }); }

    // Auto-pick a random public universe from the bot
    const universeId = await getRandomPublicUniverseId(bot.cookie, bot.robloxUserId);

    const gp = await createGamepass(bot.cookie, universeId, name, Number(price));

    return res.json({
      gampassId: String(gp.id),
      name: gp.name,
      price: gp.price,
      gampassUrl: gp.url
    });
  } catch (err) {
    console.error('gamepasses/create error:', err);
    return res.status(500).json({ error: err.message || 'Internal server error' });
  }
});

// ─── Start Server ─────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log(`RBXPAY server running on http://localhost:${PORT}`);
  console.log(`Admin dashboard: http://localhost:${PORT}/dashboard`);
});
