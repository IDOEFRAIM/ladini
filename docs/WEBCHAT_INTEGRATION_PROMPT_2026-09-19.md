# Prompt d'intégration — Chat web Ladini (sections buyer/producer)

*À copier-coller tel quel à l'agent codeur du site NextJS.*

---

## Contexte

Le backend Ladini expose désormais un canal de chat web synchrone pour les
utilisateurs déjà connectés sur le site (section `/buyer` ou `/producer`),
en plus du canal WhatsApp existant. **La conversation est PARTAGÉE avec
WhatsApp** : un utilisateur qui a déjà échangé avec l'agent sur WhatsApp
retrouve automatiquement le même contexte sur le site (et inversement), car
le numéro de téléphone est la clé unique de la conversation.

Serveur : `https://api.ladini.tech`

## ⚠️ Règle de sécurité non négociable

**Ces endpoints ne doivent JAMAIS être appelés depuis le navigateur.**

Ils sont protégés par un secret serveur unique et partagé (`X-Internal-Token`).
Ce n'est PAS une authentification par utilisateur — c'est une authentification
"ce backend a le droit de parler à Ladini". Si ce token finit dans le bundle
JS envoyé au navigateur (variable `NEXT_PUBLIC_*`, appel `fetch` côté client,
etc.), **n'importe qui pourrait usurper n'importe quel numéro de téléphone**
et lire/écrire dans la conversation WhatsApp de n'importe quel autre
utilisateur (commandes en cours, négociations, informations personnelles).

Architecture obligatoire :

```
Navigateur (composant chat)
   │  fetch("/api/chat/producer", { message })     ← PAS de token ici
   ▼
Backend Next.js (route API / server action)
   │  vérifie la session utilisateur DÉJÀ authentifiée par OTP
   │  ajoute X-Internal-Token (variable d'env SERVEUR, jamais NEXT_PUBLIC_*)
   ▼
https://api.ladini.tech/api/webchat/producer   ← token ici seulement
```

Le numéro de téléphone transmis à Ladini doit être **celui déjà vérifié par
OTP au login du site** — jamais un champ texte que l'utilisateur pourrait
modifier côté client.

## Endpoints

### `POST /api/webchat/producer`
### `POST /api/webchat/buyer`

Un endpoint par section (le rôle producteur/acheteur est déterminé par
l'endpoint appelé, pas par le contenu du message).

**Headers**
```
Content-Type: application/json
X-Internal-Token: <secret — variable d'environnement serveur uniquement>
```

**Body**
```json
{
  "message": "Je cherche 50kg de tomates près de Bobo",
  "phone_number": "+22670000001"
}
```
- `message` : texte libre de l'utilisateur, 1 à 4000 caractères.
- `phone_number` : format international (`+225...`), déjà vérifié par OTP
  côté site — obligatoire.

**Réponse `200 OK`**
```json
{
  "reply": "Voici les producteurs de tomates disponibles près de Bobo-Dioulasso...",
  "interactive": null,
  "workspace_id": "+22670000001"
}
```
- `reply` : texte à afficher directement dans la bulle de chat.
- `interactive` : `null` la plupart du temps ; parfois un objet du type
  `{"kind": "confirm"}` ou `{"kind": "quick_reply", "buttons": [...]}` —
  indique que l'agent attend une confirmation ou un choix rapide. Vous
  pouvez l'ignorer au départ (l'utilisateur peut toujours répondre en texte
  libre, ex. "oui"/"non") et l'exploiter plus tard pour afficher de vrais
  boutons.
- `workspace_id` : identifiant interne (= le numéro), pas d'usage requis
  côté UI.

**Erreurs**
| Code | Cause | Action côté site |
|---|---|---|
| 401 | Token absent/invalide | Bug de configuration serveur — ne jamais arriver en prod, logger une alerte |
| 422 | `message` vide/trop long ou `phone_number` manquant | Validation avant envoi côté site |
| 503 | Token interne non configuré côté Ladini | Réessayer plus tard, afficher "service momentanément indisponible" |
| 500 | Erreur inattendue | Réessayer une fois, sinon afficher un message d'erreur générique |

**Latence** : généralement quelques secondes. Cas rares jusqu'à ~45s
(l'agent peut faire plusieurs appels LLM/outils avant de répondre) — prévoir
un indicateur "en train d'écrire..." plutôt qu'un timeout court côté site.
Ne coupez pas la requête avant 60s.

## Exemple (route API Next.js, App Router)

```ts
// app/api/chat/[role]/route.ts
import { NextRequest, NextResponse } from "next/server";
import { getServerSession } from "@/lib/auth"; // votre système d'auth existant

export async function POST(
  req: NextRequest,
  { params }: { params: { role: "producer" | "buyer" } }
) {
  const session = await getServerSession(req);
  if (!session?.phoneNumber) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const { message } = await req.json();

  const res = await fetch(
    `https://api.ladini.tech/api/webchat/${params.role}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Token": process.env.LADINI_INTERNAL_TOKEN!, // JAMAIS NEXT_PUBLIC_
      },
      body: JSON.stringify({
        message,
        phone_number: session.phoneNumber, // vérifié par OTP au login, jamais saisi ici
      }),
    }
  );

  if (!res.ok) {
    return NextResponse.json({ error: "agent_unavailable" }, { status: 502 });
  }

  const data = await res.json();
  return NextResponse.json({ reply: data.reply, interactive: data.interactive });
}
```

Côté navigateur, le composant chat appelle uniquement `/api/chat/producer`
ou `/api/chat/buyer` (votre propre route Next.js ci-dessus) — jamais
`api.ladini.tech` directement.

## Ce qu'il vous reste à faire côté site

1. Récupérer `LADINI_INTERNAL_TOKEN` auprès de l'équipe Ladini et le
   déclarer en variable d'environnement **serveur** (pas `NEXT_PUBLIC_*`).
2. Créer la route API/server action ci-dessus, une fois par section
   (`/producer`, `/buyer`).
3. Le composant chat UI appelle cette route interne, affiche `reply` dans
   une bulle, gère un état "en train d'écrire" pendant l'attente.
4. Ne jamais afficher/logger `phone_number` ou le token côté client.
