# Onboarding — modèle géographique RÉGION (2026-10-05)

## Contrat

```
Onboarding geographic identity = Burkina region
Sub-zone is not required
```

À l'onboarding, Ladini raisonne en **régions** (17), jamais en sous-zones. Une ville ou un
chef-lieu aide à *résoudre* la région ; la valeur canonique stockée est la région.

`onboarding region` ≠ `delivery location` ≠ `weather location` ≠ `farm exact location` :
ces notions opérationnelles (zones `governance.zones`, GPS, ferme) ne changent pas.

## Source de vérité unique

`backend/src/ladini/domain/burkina_regions.py` — `REGIONS` (slug stable, nom canonique,
chef-lieu), `resolve_region()` (texte libre → région), `canonical_region()` (validation
stricte), `normalize_place()` (casse/accents/apostrophes/tirets).

| slug | Région (valeur stockée) | Chef-lieu |
|---|---|---|
| bankui | Bankui | Dédougou |
| djoro | Djôrô | Gaoua |
| goulmou | Goulmou | Fada N'Gourma |
| guiriko | Guiriko | Bobo-Dioulasso |
| kadiogo | Kadiogo | Ouagadougou |
| kuilse | Kuilsé | Kaya |
| liptako | Liptako | Dori |
| nando | Nando | Koudougou |
| nakambe | Nakambé | Tenkodogo |
| nazinon | Nazinon | Manga |
| oubri | Oubri | Ziniaré |
| sirba | Sirba | Bogandé |
| soum | Soum | Djibo |
| tannounyan | Tannounyan | Banfora |
| tapoa | Tapoa | Diapaga |
| sourou | Sourou | Tougan |
| yaadga | Yaadga | Ouahigouya |

## Flux (`agents/onboarding.py::_resolve_zone`)

1. `resolve_region(texte)` : « Ouaga », « je suis à Bobo », « dans le Nando », « djoro »… →
   région canonique (`declared_location` = nom canonique, `coverage_status` = `COVERED`).
2. Lien opérationnel : `zone_id` = ligne racine `governance.zones` portant le nom canonique, à
   défaut le chef-lieu (anciennes lignes). Sans ligne : `zone_id` NULL, région tout de même
   enregistrée — **l'onboarding n'est jamais bloqué**.
3. Plusieurs régions / « le Burkina » seul → on redemande la région (jamais Kadiogo par défaut).
4. Localité inconnue → repli historique DB (zone racine / hiérarchie), sinon on demande **une
   seule fois** « Dans quelle région se trouve X ? » ; une 2ᵉ réponse non résolue part en
   `OUT_OF_COVERAGE` (profil créé, `declared_location` conservé).

Aucune question de sous-zone n'existe dans l'onboarding. `sub_zone` n'est lue nulle part ici.

## Données existantes / migrations

- Aucune migration, aucun backfill : `users.zone_id`, `declared_location`, `coverage_status`
  existent (migration 0005). Les utilisateurs `zone=Kadiogo|Guiriko` fonctionnent inchangés.
- **Limite connue** : les 15 autres régions n'ont pas de ligne racine dans `governance.zones`
  (table gérée côté Drizzle, `climatic_region_id` obligatoire). Tant qu'un admin ne les a pas
  créées, `zone_id` reste NULL pour ces profils : les services dépendant de `zone_id`
  (matching/livraison) les traitent comme sans zone opérationnelle. Ne pas les inventer ici.
