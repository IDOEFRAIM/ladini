# Onboarding — résolution de zone au niveau région (pas ville)

**Date** : 2026-09-18
**Origine** : retour produit direct sur le flux d'onboarding WhatsApp (dogfooding).

## Constat produit

Deux problèmes remontés sur le premier contact utilisateur :

1. L'onboarding demandait la **ville ou province précise** de l'utilisateur.
   Une localité précise a beaucoup moins de chances d'être déjà répertoriée
   en base qu'une zone plus large — l'utilisateur tombait plus souvent sur
   "zone introuvable" et devait réessayer, une expérience frustrante dès le
   premier contact.
2. Le tout premier message noyait l'unique action attendue (répondre
   producteur/acheteur) sous deux paragraphes de pitch produit.

Ce document couvre uniquement le point 1 (conséquences sur le modèle de
données/logique métier) — le point 2 est une simple réécriture de texte,
voir `backend/src/ladini/agents/onboarding.py::_WELCOME`.

## Décision

La résolution de zone pendant l'onboarding se fait désormais **au niveau
région** (`governance.zones`, `parent_id IS NULL`) plutôt qu'au niveau
ville/village (`parent_id` renseigné). Alternative envisagée et écartée par
choix produit explicite : repli automatique ville → région uniquement quand
la ville n'est pas trouvée (aurait gardé la précision ville quand possible).
Le choix retenu simplifie l'expérience (une seule notion "région" à
comprendre, jamais deux) au prix de la précision géographique décrite
ci-dessous.

## Ce qui a changé (code)

- `backend/src/ladini/services/database/base.py::get_zone_by_name` — filtre
  désormais `Zone.parent_id.is_(None)` en plus du matching par similarité
  trigram.
- `backend/src/ladini/services/database/base.py::get_available_zones` — même
  filtre ; alimente le hint "Zones valides : ..." montré à l'utilisateur
  quand sa saisie ne matche rien.
- `backend/src/ladini/agents/onboarding.py` — question, messages d'erreur et
  libellés mis à jour ("région" au lieu de "ville ou province"). Les exemples
  de villes codés en dur (Ouagadougou, Bobo-Dioulasso, Koudougou) ont été
  retirés du message de repli statique : ce ne sont PAS des noms de région,
  et aucune donnée de seed réelle n'était disponible pour vérifier des noms
  de région corrects — le hint dynamique (`_build_zone_catalog_hint`,
  toujours alimenté par la vraie base) reste la seule source de noms fiable.

Aucun autre appelant de `get_zone_by_name`/`get_available_zones` n'existe
dans le code applicatif (vérifié par recherche exhaustive) — restriction
sans effet de bord ailleurs.

## Conséquence NON demandée mais découverte pendant l'implémentation

`backend/src/ladini/services/database/buyer.py` (estimation logistique,
fonction autour de la ligne 1140) classe le transport en 3 paliers :

1. **Même ville** (`producer_zone_id == buyer_zone_id`) → 1500 (circuit
   ultra-court).
2. **Même région** (`Zone.parent_id` identique des deux côtés, et non-nul)
   → 4000 (transit régional/inter-communal).
3. Sinon → 12000 (transit national/longue distance).

Avec `zone_id` désormais **toujours** au niveau région pour tout profil créé
via l'onboarding :

- Le palier 1 ("même ville") devient de facto "même région" — dégradation
  **acceptée explicitement** par le choix produit ci-dessus.
- Le palier 2 ("même région", basé sur `Zone.parent_id`) devient
  **structurellement inatteignable** : une zone région (`parent_id IS NULL`)
  n'a par définition pas de `parent_id` — la condition
  `if p_parent and p_parent == b_parent` est toujours fausse. **Ce n'est
  PAS un faux positif** (pas de bug de sécurité/facturation — `p_parent`
  falsy fait tomber dans le `else`, jamais l'inverse), seulement une
  estimation logistique **moins fine** : tout trajet inter-région tombe
  systématiquement dans le palier "longue distance" (12000), même entre deux
  régions limitrophes.

**Non corrigé dans ce chantier** (hors scope de la demande initiale,
décision produit à part entière) : si l'estimation à 3 paliers doit être
restaurée, il faudra une notion de "voisinage inter-région" indépendante de
`parent_id` (ex: table d'adjacence régionale, ou distance géographique via
`Zone.latitude`/`longitude`, déjà présents sur le modèle).

## Fichiers modifiés

- `backend/src/ladini/services/database/base.py`
- `backend/src/ladini/agents/onboarding.py`

## Tests

Voir `backend/tests/unit/test_onboarding_zone_region_level.py` (nouveau) et
la suite existante `test_onboarding_adaptive_questions.py`/
`test_onboarding_node_state_roundtrip.py` (non-régression, toujours verts).
