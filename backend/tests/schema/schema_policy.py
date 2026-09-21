"""Politique de schéma : exceptions explicites et justifiées (source unique pour les tests)."""

# Tables DÉLIBÉRÉMENT présentes dans Drizzle/PostgreSQL sans miroir Python (spécifiques au site :
# distribution de semences, gérée uniquement par le frontend). Toute autre table Drizzle DOIT avoir
# un miroir SQLAlchemy ; toute table SQLAlchemy DOIT exister dans Drizzle.
SITE_ONLY_TABLES = {
    ("marketplace", "seed_allocations"),
    ("marketplace", "seed_distributions"),
    ("marketplace", "seed_distribution_attempts"),
}
