# Prompt d'intégration — Config unités par sous-catégorie (côté site, admin)

*À copier-coller tel quel à l'agent codeur du site NextJS.*

---

## Contexte

Le backend Ladini (agent WhatsApp/webchat) devinait jusqu'ici l'unité d'un
produit (KG, TETE, LITRE...) depuis le texte libre du producteur, avec une
règle de secours codée en dur ("un élevage se compte à la tête"). Ça a
produit plusieurs incidents réels : un bœuf vendu "au litre", des poulets
vendus "en KG"...

Nouveau design, plus stable : **l'admin configure, par sous-catégorie de
produit, un ensemble d'unités autorisées + une unité PRIORITAIRE utilisée
pour standardiser** — exactement le même principe que `minimum_order_quantity`
/`minimum_order_unit`, qui existe déjà sur `governance.sub_categories` et que
vous gérez déjà côté admin (`dr-governance.service.ts::updateSubCategoryThreshold`).

Exemples concrets (donnés par le métier) :
- **Lait** → une seule unité autorisée : `LITRE` (= aussi l'unité prioritaire).
- **Bœufs** → `TETE`, `UNITE` autorisées, `TETE` prioritaire.
- **Maïs** → `KG`, `TONNE`, `SAC` autorisées, `TONNE` prioritaire (ordre de
  priorité = c'est CETTE unité qui sert de référence pour standardiser).

Le backend est **déjà prêt à consommer cette config automatiquement**, dès
que les colonnes existent — zéro redéploiement backend nécessaire de notre
côté une fois que vous avez fait la migration. Tant qu'elles n'existent pas,
le backend continue de fonctionner exactement comme avant (lecture
défensive avec repli silencieux — voir plus bas).

## Ce qu'il faut ajouter côté site (Drizzle)

Deux colonnes sur la table `governance.sub_categories` (celle qui porte déjà
`minimum_order_quantity`/`minimum_order_unit`) :

| Colonne         | Type           | Nullable | Notes |
|-----------------|----------------|----------|-------|
| `priority_unit`   | `text`         | oui      | Une des valeurs de `allowed_units`. `NULL` = pas encore configuré. |
| `allowed_units`  | `text[]`       | oui      | Ensemble d'unités autorisées pour cette sous-catégorie. `NULL`/`{}` = pas encore configuré. |

Migration Drizzle indicative (adaptez au style du projet) :

```ts
// governance/sub_categories
priority_unit: text("priority_unit"),
allowed_units: text("allowed_units").array(),
```

**Aucune contrainte SQL `CHECK`/`FK` requise côté DB** — la validation des
valeurs (liste fermée d'unités, cohérence priority ∈ allowed) doit se faire
côté service/formulaire admin (voir plus bas), sur le même modèle que la
validation existante de `minimum_order_quantity` (qui interdit déjà 0/négatif
côté web, jamais côté DB).

## Liste FERMÉE des unités valides — ⚠️ important

Le backend ne reconnaît QUE ces 7 unités canoniques aujourd'hui
(`domain/quantity_unit.py::UNIT_SYNONYMS`) :

```
KG, TONNE, SAC, PANIER, TETE, UNITE, LITRE
```

Toute autre valeur écrite dans `allowed_units`/`priority_unit` ne sera pas
détruite côté backend (elle est préservée telle quelle, jamais silencieusement
effacée), mais elle sort du système de conversion/normalisation connu — donc
**restreignez le sélecteur admin à cette liste de 7**, rien d'autre pour
l'instant. Notamment : PAS de "gramme" (`G`) séparé de `KG` aujourd'hui — si
le métier en a réellement besoin (ex. petites quantités d'épices), il faudra
nous le signaler pour l'ajouter au registre backend AVANT de l'exposer côté
admin, plutôt que de l'ajouter en silence ici.

## Sémantique exacte attendue par le backend

Le backend lit ces deux colonnes brutes, sans transformation :

```json
{
  "priority_unit": "LITRE",
  "allowed_units": ["LITRE"]
}
```

Règles de cohérence à faire respecter **côté formulaire admin** (jamais côté
DB, même politique que le champ seuil existant) :

1. `allowed_units` doit contenir **au moins une** unité de la liste fermée
   ci-dessus si la sous-catégorie est "configurée pour l'unité" — sinon
   laisser les deux colonnes `NULL` (= pas configuré, comportement backend
   inchangé).
2. `priority_unit`, si renseignée, **doit être une valeur présente dans
   `allowed_units`** — sinon le backend l'ignore silencieusement et retombe
   sur ses règles historiques (pas un crash, mais une config qui ne sert à
   rien : à bloquer dès la saisie admin plutôt que de laisser passer une
   incohérence invisible).
3. Si `allowed_units` ne contient qu'**une seule** unité, `priority_unit`
   peut être laissée vide — le backend la déduit automatiquement dans ce
   cas précis (ex. lait → `allowed_units: ["LITRE"]` suffit).

## Ce qu'il reste à faire côté site

1. **Migration** : ajouter `priority_unit`/`allowed_units` à
   `governance.sub_categories` (voir DDL indicatif ci-dessus).
2. **Écran admin** (probablement le même formulaire que celui qui gère déjà
   `minimum_order_quantity`/`minimum_order_unit` par sous-catégorie, dans
   `dr-governance.service.ts` ou équivalent) : pour chaque sous-catégorie,
   ajouter
   - un multi-select des 7 unités canoniques ("Unités autorisées") ;
   - un select radio (options limitées à ce qui est coché juste au-dessus)
     pour "Unité prioritaire (standardisation)" — désactivé/optionnel si un
     seul choix est coché.
3. **Validation** côté service d'écriture (règles 1-2 ci-dessus), même
   niveau de rigueur que la validation existante du seuil de quantité
   minimale.
4. Champs **optionnels à la création** d'une sous-catégorie — l'existant
   (des centaines de sous-catégories déjà en base) doit continuer de
   fonctionner sans y toucher : `NULL` = "pas encore configuré", jamais une
   erreur.

## Comment le backend consomme cette config (pour info, rien à faire ici)

- `services/database/base.py::get_product_category_unit_config` résout la
  sous-catégorie par similarité de nom (trigram), puis lit ces deux
  colonnes par un SELECT défensif — si elles n'existent pas encore
  (aujourd'hui), il se dégrade proprement, sans jamais faire échouer une
  conversation en cours.
- `domain/quantity_unit.py::resolve_product_unit` applique alors : unité
  écrite par le producteur ou déjà posée → honorée **seulement** si dans
  `allowed_units` ; sinon `priority_unit` s'applique pour standardiser.

Aucun nouvel endpoint HTTP n'est nécessaire de votre côté : comme pour
`minimum_order_quantity`, le backend lit directement la table partagée
(`governance.sub_categories`) — pas d'API à exposer, juste la colonne à
remplir et l'écran admin pour la remplir proprement.
