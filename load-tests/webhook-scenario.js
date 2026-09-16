// ═════════════════════════════════════════════════════════════════════
// load-tests/webhook-scenario.js — charge constante sur le webhook inbound
// (POST /api/webhook/twilio ou /api/webhook/whatsapp), à 50 / 200 / 500 VUs.
//
// ⚠️ SÉCURITÉ / COÛT — LIRE AVANT D'EXÉCUTER ⚠️
// Chaque requête qui passe la vérification de signature déclenche un VRAI
// `process_agent_task.delay(...)` côté Celery (voir
// backend/src/ladini/api/routes/twilio_webhook.py et whatsapp_webhook.py) —
// donc un VRAI appel LLM (GROQ_API_KEY facturé) et potentiellement un VRAI
// envoi WhatsApp/SMS. Ce script NE DOIT PAS être pointé sur une base
// d'identifiants de production (`GROQ_API_KEY`, `WHATSAPP_CLOUD_API_TOKEN`,
// `TWILIO_AUTH_TOKEN` réels) sans savoir exactement ce qu'on fait.
//
// DÉFAUT SÛR : si vous NE fournissez PAS `SIGNING_SECRET`, ce script envoie
// des requêtes NON SIGNÉES. `verify_twilio_signature`/
// `verify_whatsapp_cloud_signature` (backend/src/ladini/api/security.py) sont
// fail-closed : la requête est rejetée AVANT tout enqueue Celery (403/503).
// Vous mesurez alors la capacité HTTP/FastAPI/dépendances de la route (utile
// pour la couche réseau/proxy), MAIS PAS le chemin complet
// idempotence→enqueue→worker. C'est intentionnel — voir
// load-tests/real-small-scale-test.md pour le SEUL protocole documenté pour
// tester le chemin complet, à échelle volontairement minuscule et sur un
// environnement dont vous savez qu'il utilise des identifiants sandbox.
//
// Usage :
//   TARGET_URL=http://127.0.0.1:8000 SCENARIO=50  k6 run load-tests/webhook-scenario.js
//   TARGET_URL=http://127.0.0.1:8000 SCENARIO=200 k6 run load-tests/webhook-scenario.js
//   TARGET_URL=http://127.0.0.1:8000 SCENARIO=500 k6 run load-tests/webhook-scenario.js
//
// Variables d'environnement :
//   TARGET_URL       URL de base de l'API (def: http://127.0.0.1:8000 — JAMAIS
//                     une URL de prod par défaut).
//   PROVIDER          twilio | whatsapp_cloud (def: twilio).
//   SCENARIO          50 | 200 | 500 | all (def: 50) — quel(s) palier(s) VUs lancer.
//   DURATION           durée de charge constante par palier (def: 30s).
//   SIGNING_SECRET     TWILIO_AUTH_TOKEN (provider=twilio) ou WHATSAPP_APP_SECRET
//                       (provider=whatsapp_cloud) — OMIS par défaut (voir plus haut).
//   TWILIO_PUBLIC_BASE_URL  si l'API cible valide la signature Twilio contre une
//                       URL publique précise (voir security.py::_candidate_signed_urls) ;
//                       sinon TARGET_URL est utilisé tel quel pour signer.
//
// Voir load-tests/README.md pour l'interprétation des métriques capturées.
// ═════════════════════════════════════════════════════════════════════

import http from "k6/http";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";
import crypto from "k6/crypto";
import { randomIntBetween } from "https://jslib.k6.io/k6-utils/1.2.0/index.js";

const TARGET_URL = (__ENV.TARGET_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const PROVIDER = __ENV.PROVIDER || "twilio"; // twilio | whatsapp_cloud
const SCENARIO = __ENV.SCENARIO || "50"; // 50 | 200 | 500 | all
const DURATION = __ENV.DURATION || "30s";
const SIGNING_SECRET = __ENV.SIGNING_SECRET || ""; // volontairement vide par défaut
const SIGN_URL_BASE = (__ENV.TWILIO_PUBLIC_BASE_URL || TARGET_URL).replace(/\/$/, "");

// ── Métriques custom — en plus des http_req_duration/http_req_failed natifs
// de k6 (déjà dans le résumé standard, voir README "où lire chaque métrique").
const webhookRejected = new Counter("ladini_webhook_rejected_total"); // 403/503 attendu si non signé
const webhookAccepted = new Counter("ladini_webhook_accepted_total"); // 200 → enqueue réel
const webhookDuration = new Trend("ladini_webhook_duration_ms", true);

// ── Construction des scenarios k6 — un palier par valeur de SCENARIO.
// Executor `constant-vus` : VUs fixes pendant DURATION — mesure la capacité
// en régime établi (pas une rampe). Voir burst-scenario.js pour l'absorption
// de pics.
function buildScenarios(which) {
  const defs = {
    "50": { vus: 50, executor: "constant-vus" },
    "200": { vus: 200, executor: "constant-vus" },
    "500": { vus: 500, executor: "constant-vus" },
  };
  const scenarios = {};
  const keys = which === "all" ? Object.keys(defs) : [which];
  let startOffset = 0;
  for (const k of keys) {
    if (!defs[k]) continue;
    scenarios[`vus_${k}`] = {
      executor: "constant-vus",
      vus: defs[k].vus,
      duration: DURATION,
      startTime: `${startOffset}s`,
      exec: "webhookIteration",
      tags: { vus_target: k },
    };
    // enchaîne les paliers avec 10s de battement (drain des dernières requêtes)
    startOffset += parseDurationSeconds(DURATION) + 10;
  }
  return scenarios;
}

function parseDurationSeconds(d) {
  const m = /^(\d+)s$/.exec(d) || /^(\d+)m$/.exec(d);
  if (!m) return 30;
  return d.endsWith("m") ? parseInt(m[1], 10) * 60 : parseInt(m[1], 10);
}

export const options = {
  scenarios: buildScenarios(SCENARIO),
  // Seuils indicatifs (pas des gates CI ici — un k6 exit != 0 en cas de
  // dépassement resterait informatif tant qu'aucune pipeline n'y est câblée) :
  // le webhook DOIT rester rapide (il ne fait qu'enqueue, voir docstring
  // twilio_webhook.py bloc 6) même sous charge — s'il ne l'est pas plus, la
  // file Celery ne joue plus son rôle d'amortisseur.
  thresholds: {
    http_req_duration: ["p(95)<1000", "p(99)<2000"],
    ladini_webhook_duration_ms: ["p(95)<1000"],
  },
};

// ── Générateurs de payload — un numéro/MessageSid/wamid DIFFÉRENT à CHAQUE
// itération : sinon l'idempotence Redis (`msg:{MessageSid}` / `msg:{wamid}`,
// voir twilio_webhook.py bloc 1 / whatsapp_webhook.py bloc 1) dédoublonne
// tout sur une seule clé et on ne mesure plus qu'UN SEUL message traité en
// boucle, pas une vraie charge.
function fakePhoneE164() {
  // Préfixe +221 (Sénégal, cohérent avec le marché cible Ladini) + 9 chiffres
  // aléatoires — jamais un vrai numéro assigné.
  return `+221${randomIntBetween(700000000, 799999999)}`;
}

function randomHex(len) {
  let s = "";
  const chars = "0123456789abcdef";
  for (let i = 0; i < len; i++) s += chars[randomIntBetween(0, 15)];
  return s;
}

const SAMPLE_BODIES = [
  "Bonjour",
  "Je veux vendre 50 kg de maïs",
  "photos maïs",
  "1",
  "combien coûte le sac d'oignons ?",
  "confirmer",
];

function sampleBody() {
  return SAMPLE_BODIES[randomIntBetween(0, SAMPLE_BODIES.length - 1)];
}

// ── Signature Twilio — miroir EXACT de twilio.request_validator.RequestValidator
// (voir backend/src/ladini/api/security.py::verify_twilio_signature) :
// base64(HMAC-SHA1(url + concat(sorted(key+value)), authToken)).
function twilioSignature(url, params, authToken) {
  let data = url;
  Object.keys(params)
    .sort()
    .forEach((k) => {
      data += k + params[k];
    });
  return crypto.hmac("sha1", authToken, data, "base64");
}

// ── Signature WhatsApp Cloud — miroir de verify_whatsapp_cloud_signature :
// "sha256=" + hex(HMAC-SHA256(corps_brut, app_secret)).
function whatsappSignature(rawBody, appSecret) {
  return "sha256=" + crypto.hmac("sha256", appSecret, rawBody, "hex");
}

function buildTwilioRequest() {
  const messageSid = "SM" + randomHex(32);
  const params = {
    From: `whatsapp:${fakePhoneE164()}`,
    Body: sampleBody(),
    MessageSid: messageSid,
  };
  const url = `${SIGN_URL_BASE}/api/webhook/twilio`;
  const headers = { "Content-Type": "application/x-www-form-urlencoded" };
  if (SIGNING_SECRET) {
    headers["X-Twilio-Signature"] = twilioSignature(url, params, SIGNING_SECRET);
  }
  return {
    url: `${TARGET_URL}/api/webhook/twilio`,
    body: params, // k6 encode un objet en x-www-form-urlencoded automatiquement
    params: { headers },
  };
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
              metadata: {
                display_phone_number: "221770000000",
                phone_number_id: "1234567890",
              },
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
  if (SIGNING_SECRET) {
    headers["X-Hub-Signature-256"] = whatsappSignature(rawBody, SIGNING_SECRET);
  }
  return {
    url: `${TARGET_URL}/api/webhook/whatsapp`,
    body: rawBody,
    params: { headers },
  };
}

export function webhookIteration() {
  const req = PROVIDER === "whatsapp_cloud" ? buildWhatsAppRequest() : buildTwilioRequest();
  const res = http.post(req.url, req.body, req.params);

  webhookDuration.add(res.timings.duration);
  if (res.status === 200) {
    webhookAccepted.add(1);
  } else if (res.status === 403 || res.status === 503) {
    webhookRejected.add(1);
  }

  check(res, {
    "réponse reçue (pas de timeout/erreur réseau)": (r) => r.status !== 0,
    "pas de 5xx applicatif (hors 503 fail-closed attendu si secret absent)": (r) =>
      r.status < 500 || r.status === 503,
  });
}

export default webhookIteration;
