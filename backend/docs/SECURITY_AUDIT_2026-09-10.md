# Audit de sécurité — 2026-09-10

Périmètre : `backend/src/ladini` (~79 000 lignes). Objectif : trouver et
corriger les failles **réellement exploitables par un attaquant**, pas produire
une liste de bonnes pratiques.

Résultat : **5 vulnérabilités exploitables corrigées** (2 critiques, 2 élevées,
1 moyenne), verrouillées par **21 nouveaux tests**. Suite complète :
**3468 tests, 0 échec** (base : 3447, 0 échec — aucune régression).

---

## Le fil conducteur

Tout le système dérive l'identité de l'utilisateur d'**un numéro de téléphone
reçu sur le réseau**. Il n'existe ni session, ni mot de passe, ni jeton
utilisateur : « être `+226…` » *est* l'authentification. Cette conception est
correcte pour un agent WhatsApp — **à condition** que chaque porte d'entrée
prouve que le numéro vient bien de l'opérateur de messagerie.

Trois portes sur quatre ne le prouvaient pas. Chacune suffisait seule à une
usurpation d'identité complète : passer et confirmer des commandes, désigner le
gagnant d'une enchère, vider un stock, annuler, modifier le catalogue — au nom
de n'importe quel utilisateur.

---

## P0-1 · `POST /api/webhook/twilio` sans aucune vérification de signature

**Exploitabilité : immédiate, sans authentification, une seule requête HTTP.**

`verify_twilio_signature` existait bien dans `api/security.py`, et
`api/README.md` affirmait qu'elle « intercepte la requête pour valider
l'authenticité de Twilio ». Elle n'était câblée sur **aucune route** :
`main.py` faisait un `include_router(twilio_router)` nu, et le router ne
déclarait aucune dépendance. Du code de sécurité présent, documenté, et
totalement inerte.

```
POST /api/webhook/twilio
From=whatsapp:%2B22670000000&Body=je+confirme&MessageSid=SM<aléatoire>
```

→ le tour d'agent s'exécute comme ce numéro. `MediaUrl0` donnait en prime une
primitive SSRF (voir P2-1).

**Correctif** — `routes/twilio_webhook.py` : dépendance au niveau du **router**
(pas de la route), pour que toute route ajoutée plus tard en hérite.

## P0-2 · `/api/market/*` entièrement ouvertes

**Exploitabilité : immédiate, plus directe encore que P0-1** (JSON simple, pas
de format Twilio à imiter).

```
POST /api/market/producer   {"phone_number": "+226…", "message": "…"}
```

`process_agent_task.delay(..., force_role=True)` — aucune authentification.
`GET /api/market/status/{task_id}` renvoyait en plus le résultat de n'importe
quelle tâche, donc la **réponse conversationnelle d'un autre utilisateur**,
ainsi que le message brut des exceptions (fragments d'URL de connexion, de
requête SQL, de payload).

**Correctif** — nouveau `INTERNAL_API_TOKEN` (header `X-Internal-Token`,
comparaison en temps constant), appliqué au router. Fail-closed : secret non
configuré → 503 pour tout le monde, jamais « ouvert par défaut ». Même posture
que `ADMIN_API_TOKEN`, déjà en place. `/status` ne renvoie plus que le *type*
de l'exception.

## P1-1 · Le webhook WhatsApp Cloud était fail-**open**

`WHATSAPP_APP_SECRET` absent → un `logger.warning`, **puis traitement du
message quand même**. Comme `whatsapp_cloud` est le `MESSAGING_PROVIDER` par
défaut, un secret oublié en production laissait le webhook **principal** grand
ouvert, avec pour seule trace une ligne de log parmi les autres.

**Correctif** — fail-closed (503), sauf en développement **déclaré**
(`ENV=development`), qui journalise bruyamment à chaque requête. Un oubli de
configuration ne peut plus se traduire par un endpoint public.

## P1-2 · Code de livraison (escrow) forçable par force brute → déblocage de fonds

**La seule faille de cet audit qui provoque une perte financière directe.**

`EscrowMixin.verify_delivery_otp` est le seul chemin qui fait passer une
commande en `PAID_OUT`/`DELIVERED`, c'est-à-dire qui **débloque les fonds
séquestrés**. Il n'avait **aucun compteur de tentatives** sur un code de
**4 chiffres** — 10 000 possibilités. Un producteur pouvait deviner un code et
encaisser sans que l'acheteur ait jamais confirmé la livraison.

Deux aggravations s'ajoutaient :

1. La requête cherchait le code parmi **toutes** les commandes `ESCROWED` du
   producteur à la fois. Avec N livraisons en cours, **un essai testait N codes
   simultanément** : l'espace de recherche effectif était divisé par N.
2. Un code validé **restait en base**, donc rejouable sur une commande future.

Le seul frein existant était `ToolRateLimiter` (60 appels/min, en mémoire du
process) — un plafond de débit, pas un verrouillage : 10 000 essais en moins de
3 heures, remis à zéro à chaque redéploiement.

**Correctif** — verrouillage **persisté en base** (`delivery_otp_attempts`,
`delivery_otp_locked_until` sur `marketplace.orders`, DDL idempotent dans
`SCHEMA_COLUMN_DDL`) : 5 essais puis 15 minutes de blocage, soit ~20 essais/heure
(~250 heures d'attaque continue pour une chance sur deux). Un essai raté est
comptabilisé sur **chacune** des commandes candidates — la comptabilité fidèle
de ce qui vient d'être tenté. Une commande verrouillée refuse **même le bon
code** (sinon le verrou n'en est pas un). Le verrou est temporaire : un
producteur légitime qui se trompe ne perd pas l'accès à ses fonds. Un code
validé est effacé. Un nouveau code remet le compteur à zéro.

Le compteur est en Postgres et non en Redis/mémoire **délibérément** : il doit
survivre à un redéploiement et être partagé par tous les workers Celery.

> **Non corrigé, assumé :** l'entropie du code reste de 4 chiffres. Passer à 6
> toucherait la validation « exactement 4 chiffres », l'extraction dans
> `flows/producer/flow.py`, les prompts, et invaliderait les codes des
> commandes `ESCROWED` en cours. C'est le **verrouillage** qui porte la
> sécurité ici — c'est écrit dans la docstring de `_generate_otp` pour que
> personne ne supprime l'un en supposant que l'autre suffit.

## P2-1 · SSRF via `MediaUrl0`

`download_twilio_media` recevait `media_url` **tel quel** depuis le formulaire
webhook et le téléchargeait. Le corps de la réponse d'une URL choisie par
l'appelant (métadonnées cloud sur `169.254.169.254`, service interne sur
localhost) était récupéré par le worker, publié dans le bucket Supabase, puis
renvoyé à l'utilisateur — une SSRF avec exfiltration.

Corriger P0-1 ferme l'accès, mais laissait la primitive intacte.

**Correctif** — liste blanche sur l'hôte **initial** (`.twilio.com`,
`.twiliocdn.com`) + `https` obligatoire. Seul l'hôte initial est vérifié :
Twilio répond 307 vers son CDN, et httpx retire l'en-tête `Authorization` au
changement d'origine. Le chemin WhatsApp Cloud n'est pas concerné (Meta
transmet des identifiants de média, pas des URL).

## P2-2 · Fuite de données personnelles dans les logs

En cas d'échec de signature, `api/security.py` journalisait en `ERROR` la
totalité des `received_params` (corps du message, numéro, coordonnées GPS) et
la signature reçue — du contenu **choisi par l'attaquant**, déversé dans les
logs à chaque tentative. Le repli de reconstruction d'URL était par ailleurs un
**domaine ngrok de développement codé en dur** dans le source.

**Correctif** — seules les *clés* des paramètres sont journalisées. L'URL signée
est résolue par `TWILIO_PUBLIC_BASE_URL` → en-têtes `X-Forwarded-Proto`/`Host`
→ URL brute de la requête ; plus aucune valeur en dur.

---

## Vérifié et jugé sain (pas de correctif)

- **Injection SQL** — aucune. Tout passe par l'ORM SQLAlchemy ; zéro
  interpolation de chaîne dans une requête sur les 79 000 lignes.
- **Exécution de code** — aucun `eval`, `exec`, `pickle`, `yaml.load`,
  `subprocess`, `os.system`, `shell=True`. Les usages sont des
  `ast.literal_eval` (sûr).
- **Celery** — sérialisation JSON (`task_serializer`/`accept_content`), donc
  pas de désérialisation d'objets arbitraires depuis le broker.
- **IPN Paydunya** — non signé côté Paydunya, mais correct par conception : le
  handler n'extrait qu'un `invoice_token` et **re-confirme statut et montant
  serveur-à-serveur** avec nos clés avant toute écriture. Le corps n'est jamais
  une source de vérité.
- **Daemon MCP HTTP** — jeton porteur, comparaison en temps constant, limite de
  taille de corps, `127.0.0.1` par défaut, `CRITICAL` au démarrage si le secret
  manque. Reste fail-open sans secret : compromis documenté et assumé, pas une
  régression de cet audit.
- **CORS** — déjà durci (pas de `allow_origins=["*"]` avec `credentials`).
- **OTP** — généré par `secrets` (CSPRNG), jamais `random`. Masqué avant
  journalisation (`infrastructure/mcp/utils.py`).
- **Secrets dans les logs** — aucun. `REDIS_URL` passe par un `_redact`.
- **`TOOL_SCOPE_MAP`** — fail-closed effectif (audits antérieurs).

## Hors périmètre / non traité

- `backend/src/futur` (~code de travaux futurs) et `backend/src/agriconnect`
  (supprimé sur cette branche) n'ont pas été audités.
- `/metrics` (scrape Prometheus) reste non authentifié : métriques agrégées,
  aucun contenu utilisateur. À placer derrière le réseau interne côté
  déploiement.
- `/docs` et `/openapi.json` restent publics — à désactiver en production
  (`FastAPI(docs_url=None)`) si la surface doit être réduite.
- Aucun test de pénétration réel n'a été exécuté contre un déploiement : les
  vulnérabilités ci-dessus sont établies par lecture du code et par des tests
  qui reproduisent le comportement, pas par exploitation en environnement live.

---

## Configuration à renseigner avant déploiement

Deux nouvelles variables (voir `.env.example` et `backend/.env.example`).
**Les deux sont fail-closed : non renseignées, les routes concernées répondent
503 au lieu de s'ouvrir.**

| Variable | Rôle | Si vide |
|---|---|---|
| `INTERNAL_API_TOKEN` | Auth de `/api/market/*` | Routes désactivées (503) |
| `TWILIO_PUBLIC_BASE_URL` | URL publique exacte signée par Twilio | Repli sur `X-Forwarded-Proto`/`Host` puis URL brute |

À vérifier comme **déjà renseignées** en production, faute de quoi le trafic
sera désormais refusé (c'était précisément le problème : il était accepté) :

- `TWILIO_AUTH_TOKEN` — sinon `/api/webhook/twilio` → 503
- `WHATSAPP_APP_SECRET` — sinon `/api/webhook/whatsapp` → 503

## Tests ajoutés (21)

- `tests/architecture/test_http_entrypoints_are_authenticated.py` (9) —
  invariant anti-dérive : **toute** route hors liste blanche explicite doit
  porter une authentification. Ajouter une route sensible sans auth fait
  échouer la suite. Vérifie aussi le comportement fail-closed réel des trois
  portes.
- `tests/unit/test_verify_delivery_otp_bruteforce.py` (12) — compteur,
  verrouillage, refus du bon code pendant le verrou, expiration du verrou,
  non-rejouabilité, imputation sur toutes les commandes candidates.

Les deux fichiers ont été validés **par réversion** : le correctif retiré, ils
échouent ; remis, ils passent. Ils ne sont pas décoratifs.
