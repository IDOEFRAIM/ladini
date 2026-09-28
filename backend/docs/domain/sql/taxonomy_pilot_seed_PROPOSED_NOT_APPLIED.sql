-- Phase B1 — PATCH DE DONNÉES PROPOSÉ, **NON APPLIQUÉ**.
-- Ne jamais exécuter automatiquement ni en production sans relecture humaine explicite.
-- N'écrit que là où AUCUNE configuration admin n'existe (priority_unit ET allowed_units NULL) :
-- une valeur déjà saisie par un admin n'est jamais écrasée.
-- Unités = registre `quantity_unit.VALID_UNITS` : KG, LITRE, PANIER, SAC, TETE, TONNE, UNITE.
-- (`measurement_family` n'existe pas en base : elle est dérivée par `analytics.units.measurement_family_of`.)
--
-- Patterns en MOTS ENTIERS (\m…\M) : « laitue » ne doit JAMAIS être lue comme « lait ».
-- `livestock = true` : un produit DÉRIVÉ d'un animal (« lait de vache », œufs, fromage…) n'est jamais
-- compté à la tête, même si son nom contient « vache » — ces lignes-là l'excluent explicitement.
BEGIN;

WITH pilot(pattern, prio, allowed, livestock) AS (
  VALUES
    ('\m(lait)s?\M',                       'LITRE', ARRAY['LITRE'],                   false),
    ('\m(huile)s?\M',                      'LITRE', ARRAY['LITRE'],                   false),
    ('\m(bovin|boeuf|bœuf|vache)s?\M',     'TETE',  ARRAY['TETE','UNITE'],            true),
    ('\m(ovin|mouton)s?\M',                'TETE',  ARRAY['TETE','UNITE'],            true),
    ('\m(caprin|chevre|chèvre)s?\M',       'TETE',  ARRAY['TETE','UNITE'],            true),
    ('\m(volaille|poulet|coq|poule)s?\M',  'TETE',  ARRAY['TETE','UNITE'],            true),
    ('\m(oeuf|œuf)s?\M',                   'UNITE', ARRAY['UNITE','PANIER'],          false),
    ('\m(fromage)s?\M',                    'KG',    ARRAY['KG','UNITE'],              false),
    ('\m(mais|maïs|riz)s?\M',              'KG',    ARRAY['KG','TONNE','SAC'],        false),
    ('\m(tomate|oignon)s?\M',              'KG',    ARRAY['KG','TONNE','SAC','PANIER'], false)
)
UPDATE governance.sub_categories s
   SET priority_unit = p.prio,
       allowed_units = p.allowed
  FROM pilot p
 WHERE s.name ~* p.pattern
   AND (NOT p.livestock
        OR s.name !~* '\m(lait|oeuf|œuf|fromage|viande|peau|cuir|beurre|yaourt)')
   AND s.priority_unit IS NULL
   AND s.allowed_units IS NULL;

-- Vérifier qu'aucune sous-catégorie n'est visée par deux motifs contradictoires AVANT d'appliquer :
--   SELECT s.name, count(*) FROM governance.sub_categories s JOIN pilot ... GROUP BY 1 HAVING count(*) > 1;
-- Relire le nombre de lignes affectées, puis COMMIT ou ROLLBACK à la main.
ROLLBACK;  -- volontairement ROLLBACK par défaut
