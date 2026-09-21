# Matrice de divergence — après nettoyage

Éléments identiques sur les 3 sources : **788** — divergents : **3**

| table | élément | drizzle | sqlalchemy | postgres | statut | action recommandée |
|---|---|---|---|---|---|---|
| marketplace.seed_allocations | (table) | présente | ABSENTE | présente | DIVERGENT | Table Drizzle sans modèle Python : OK si non utilisée par Python (seed/site), sinon ajouter le miroir  [D=✓ S=✗ P=✓] |
| marketplace.seed_distribution_attempts | (table) | présente | ABSENTE | présente | DIVERGENT | Table Drizzle sans modèle Python : OK si non utilisée par Python (seed/site), sinon ajouter le miroir  [D=✓ S=✗ P=✓] |
| marketplace.seed_distributions | (table) | présente | ABSENTE | présente | DIVERGENT | Table Drizzle sans modèle Python : OK si non utilisée par Python (seed/site), sinon ajouter le miroir  [D=✓ S=✗ P=✓] |
