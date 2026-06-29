/**
 * WhatsApp bridge for alerts-to-whatsapp.
 *
 * Holds a persistent WhatsApp-Web connection (via Baileys) and exposes a tiny
 * HTTP API on localhost that the Python script posts to. This replaces the
 * paid Whapi/Wassenger providers with a free, self-hosted transport.
 *
 *   node index.js            # first run prints a QR to link your number
 *
 * Endpoints (bound to 127.0.0.1 only):
 *   GET  /health   -> { ready }                     is WhatsApp connected?
 *   GET  /groups   -> { groups: [{id, subject}] }   list joined groups + JIDs
 *   POST /send     <- { to, body }                  send text to a JID
 *
 * Auth (the linked-device session) is persisted under ./auth so you only scan
 * the QR once. Delete that folder to force a fresh link.
 */

const http = require('http');
const path = require('path');
const {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require('baileys');
const qrcode = require('qrcode-terminal');
const pino = require('pino');

const PORT = parseInt(process.env.BRIDGE_PORT || '3000', 10);
const AUTH_DIR = process.env.BRIDGE_AUTH_DIR || path.join(__dirname, 'auth');
const logger = pino({ level: process.env.BRIDGE_LOG || 'silent' });

let sock = null;
let isReady = false;

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    logger,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    browser: ['alerts-to-whatsapp', 'Chrome', '1.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      console.log('\nScan in WhatsApp > Settings > Linked Devices > Link a device:\n');
      qrcode.generate(qr, { small: true });
    }
    if (connection === 'open') {
      isReady = true;
      console.log('[bridge] connected to WhatsApp');
    } else if (connection === 'close') {
      isReady = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      if (loggedOut) {
        console.log('[bridge] logged out — delete the auth/ folder and re-run to re-link');
      } else {
        console.log(`[bridge] connection closed (code=${code}); reconnecting...`);
        start();
      }
    }
  });
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; });
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); } catch (e) { reject(e); }
    });
    req.on('error', reject);
  });
}

function reply(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(obj));
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') {
      return reply(res, 200, { ready: isReady });
    }
    if (req.method === 'GET' && req.url === '/groups') {
      if (!isReady) return reply(res, 503, { error: 'not connected yet' });
      const all = await sock.groupFetchAllParticipating();
      const groups = Object.values(all).map((g) => ({ id: g.id, subject: g.subject }));
      return reply(res, 200, { groups });
    }
    if (req.method === 'POST' && req.url === '/send') {
      if (!isReady) return reply(res, 503, { error: 'not connected yet' });
      const body = await readJson(req);
      if (!body.to || !body.body) return reply(res, 400, { error: 'need {to, body}' });
      await sock.sendMessage(body.to, { text: body.body });
      return reply(res, 200, { ok: true });
    }
    reply(res, 404, { error: 'not found' });
  } catch (e) {
    reply(res, 500, { error: String((e && e.message) || e) });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[bridge] HTTP listening on http://127.0.0.1:${PORT}`);
});

start().catch((e) => { console.error('[bridge] fatal:', e); process.exit(1); });
