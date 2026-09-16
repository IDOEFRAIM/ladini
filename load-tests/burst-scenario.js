// ═════════════════════════════════════════════════════════════════════
// load-tests/burst-scenario.js — rampe 10 → 50 → 200 → 500 VUs pour vérifier
// que Celery joue bien son rôle d'AMORTISSEUR ("amortisseur") de pic.
//
// Prémisse vérifiée dans le code avant d'écrire ce script (backend/src/ladini/
// api/routes/twilio_webhook.py et whatsapp_webhook.py, bloc "DÉLÉGATION À
// CELERY") : le handler webhook ne fait QUE (1) checker l'idempotence Redis,
// (2) résoudre le workspace/rôle (lecture DB rapide), (3) `process_agent_task
// .delay(...)` — un enqueue Celery, PAS un appel synchrone au LLM/graphe — et
// répond immédiatement (TwiML vide / "EVENT_RECEIVED"). Le travail lourd
// (LLM, WhatsApp send) se fait dans le WORKER, en asynchrone. Donc l'attente
// de CE script est : la latence HTTP du webhook doit rester quasi plate même
// quand les VUs montent à 500 — SEULE la profondeur de la file Celery (mesurée
// via Flower/Prometheus côté worker, PAS par ce script) doit grossir. Si la
// latence HTTP grimpe avec les VUs, quelque chose bloque de façon synchrone
// dans le webhook (régression à investiguer, pas un comportement attendu).
//
// ⚠️ MÊME AVERTISSEMENT SÉCURITÉ/COÛT que webhook-scenario.js — lire son
// en-tête avant d'exécuter. Par défaut (SIGNING_SECRET absent), les requêtes
// sont rejetées AVANT enqueue (403/503) : mesure la latence HTTP de la couche
// rejet, pas le pipeline complet. Voir load-tests/real-small-scale-test.md
// pour le protocole à petite échelle et coût borné sur le pipeline complet.
//
// Usage :
//   TARGET_URL=http://127.0.0.1:8000 k6 run load-tests/burst-scenario.js
//
// Variables d'environnement : mêmes que webhook-scenario.js (TARGET_URL,
// PROVIDER, SIGNING_SECRET, TWILIO_PUBLIC_BASE_URL) — SCENARIO/DURATION ne
// s'appliquent pas ici (la rampe est fixe, voir STAGES ci-dessous).
// ═════════════════════════════════════════════════════════════════════

import http from "k6/http";
import { check } from "k6";
import { Trend, Counter } from "k6/metrics";
import crypto from "k6/crypto";
import { randomIntBetween } from "https://jslib.k6.io/k6-utils/1.2.0/index.js";

const TARGET_URL = (__ENV.TARGET_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const PROVIDER = __ENV.PROVIDER || "twilio";
const SIGNING_SECRET = __ENV.SIGNING_SECRET || "";
const SIGN_URL_BASE = (__ENV.TWILIO_PUBLIC_BASE_URL || TARGET_URL).replace(/\/$/, "");

const webhookDuration = new Trend("ladini_webhook_duration_ms", true);
const webhookAccepted = new Counter("ladini_webhook_accepted_total");
const webhookRejected = new Counter("ladini_webhook_rejected_total");

// ── Rampe 10 → 50 → 200 → 500 VUs, paliers de 30s, retour à 0 en fin de test
// (drain propre plutôt qu'un arrêt sec qui compterait des itérations en vol
// comme des échecs).
export const options = {
  scenarios: {
    burst: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "20s", target: 10 },
        { duration: "30s", target: 10 },
        { duration: "20s", target: 50 },
        { duration: "30s", target: 50 },
        { duration: "20s", target: 200 },
        { duration: "30s", target: 200 },
        { duration: "20s", target: 500 },
        { duration: "40s", target: 500 },
        { duration: "20s", target: 0 },
      ],
      exec: "burstIteration",
    },
  },
  thresholds: {
    // La thèse "Celery amortit" est fausse si la latence HTTP explose avec
    // les VUs — seuil volontairement généreux (p95<1.5s) : le but n'est pas
    // un SLA strict ici mais de détecter une dérive flagrante (le webhook
    // devient synchrone/bloquant) pendant le pic à 500 VUs.
    http_req_duration: ["p(95)<1500"],
  },
};

function fakePhoneE164() {
  return `+221${randomIntBetween(700000000, 799999999)}`;
}
function randomHex(len) {
  let s = "";
  const chars = "0123456789abcdef";
  for (let i = 0; i < len; i++) s += chars[randomIntBetween(0, 15)];
  return s;
}
const SAMPLE_BODIES = ["Bonjour", "Je veux vendre 50 kg de maïs", "photos maïs", "1", "combien coûte le sac d'oignons ?"];
function sampleBody() {
  return SAMPLE_BODIES[randomIntBetween(0, SAMPLE_BODIES.length - 1)];
}
function twilioSignature(url, params, authToken) {
  let data = url;
  Object.keys(params)
    .sort()
    .forEach((k) => {
      data += k + params[k];
    });
  return crypto.hmac("sha1", authToken, data, "base64");
}
function whatsappSignature(rawBody, appSecret) {
  return "sha256=" + crypto.hmac("sha256", appSecret, rawBody, "hex");
}

function buildTwilioRequest() {
  const messageSid = "SM" + randomHex(32);
  const params = { From: `whatsapp:${fakePhoneE164()}`, Body: sampleBody(), MessageSid: messageSid };
  const url = `${SIGN_URL_BASE}/api/webhook/twilio`;
  const headers = { "Content-Type": "application/x-www-form-urlencoded" };
  if (SIGNING_SECRET) headers["X-Twilio-Signature"] = twilioSignature(url, params, SIGNING_SECRET);
  return { url: `${TARGET_URL}/api/webhook/twilio`, body: params, params: { headers } };
}

function buildWhatsAppRequest() {
  const wamid = "wamid.HB" + randomHex(40);
  const payload = {
    object: "whatsapp_business_account",
    entry: [
      {
        id: "0000000000000000",
        changes: [
          {
            value: {
              messaging_product: "whatsapp",
              metadata: { display_phone_number: "221770000000", phone_number_id: "1234567890" },
              contacts: [{ profile: { name: "LoadTest" }, wa_id: fakePhoneE164().replace("+", "") }],
              messages: [
                {
                  from: fakePhoneE164().replace("+", ""),
                  id: wamid,
                  timestamp: `${Math.floor(Date.now() / 1000)}`,
                  type: "text",
                  text: { body: sampleBody() },
                },
              ],
            },
            field: "messages",
          },
        ],
      },
    ],
  };
  const rawBody = JSON.stringify(payload);
  const headers = { "Content-Type": "application/json" };
  if (SIGNING_SECRET) headers["X-Hub-Signature-256"] = whatsappSignature(rawBody, SIGNING_SECRET);
  return { url: `${TARGET_URL}/api/webhook/whatsapp`, body: rawBody, params: { headers } };
}

export function burstIteration() {
  const req = PROVIDER === "whatsapp_cloud" ? buildWhatsAppRequest() : buildTwilioRequest();
  const res = http.post(req.url, req.body, req.params);

  webhookDuration.add(res.timings.duration);
  if (res.status === 200) webhookAccepted.add(1);
  else if (res.status === 403 || res.status === 503) webhookRejected.add(1);

  check(res, {
    "réponse reçue": (r) => r.status !== 0,
    "latence < 2s (le webhook ne doit JAMAIS attendre le LLM)": (r) => r.timings.duration < 2000,
  });
}

export default burstIteration;
