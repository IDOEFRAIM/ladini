# Matrice de divergence — état initial (baseline)

Éléments identiques sur les 3 sources : **539** — divergents : **272**

| table | élément | drizzle | sqlalchemy | postgres | statut | action recommandée |
|---|---|---|---|---|---|---|
| auth.accounts | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| auth.accounts | col:provider | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.accounts | col:provider_account_id | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.accounts | col:scope | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.accounts | col:session_state | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.accounts | col:token_type | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.accounts | col:type | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.accounts | fk:user_id | ABSENTE | →auth.users(id) del=CASCADE upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| auth.accounts | unique:provider,provider_account_id | oui | ABSENTE | oui | DIVERGENT | Aligner sur Drizzle |
| auth.accounts | index:user_id | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| auth.sessions | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| auth.sessions | col:session_token | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.sessions | fk:user_id | ABSENTE | →auth.users(id) del=CASCADE upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| auth.sessions | index:user_id | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| auth.users | col:account_status | text NOT NULL DEFAULT 'ACTIVE' | varchar NOT NULL DEFAULT 'ACTIVE' | text NOT NULL DEFAULT 'ACTIVE' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:cnib_number | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:email | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| auth.users | col:identity_verified | bool NULL DEFAULT false | bool NULL | bool NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| auth.users | col:image | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:name | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:password | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:phone | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:role | text NOT NULL DEFAULT 'USER' | varchar NOT NULL | text NOT NULL DEFAULT 'USER' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| auth.users | col:whatsapp_enabled | bool NULL DEFAULT true | bool NULL | bool NULL DEFAULT true | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| auth.users | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| auth.users | index:created_at | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| auth.users | index:role | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| auth.users | index:zone_id | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| governance.categories | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.climatic_regions | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.organizations | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.organizations | col:status | text NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | text NOT NULL DEFAULT 'PENDING' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.organizations | col:tax_id | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.organizations | col:type | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.overlay_layers | col:enabled | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| governance.overlay_layers | col:key | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.overlay_layers | col:label | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.overlay_layers | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.prohibited_terms | col:category | text NOT NULL DEFAULT 'ILLICIT' | varchar NOT NULL DEFAULT 'ILLICIT' | text NOT NULL DEFAULT 'ILLICIT' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.prohibited_terms | col:severity | text NOT NULL DEFAULT 'HIGH' | varchar NOT NULL DEFAULT 'HIGH' | text NOT NULL DEFAULT 'HIGH' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.prohibited_terms | col:term | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.role_definitions | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.role_definitions | col:permissions | text[] NOT NULL DEFAULT '{}' | varchar[] NOT NULL DEFAULT '{}' | text[] NOT NULL DEFAULT '{}' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.standard_prices | col:unit | text NOT NULL DEFAULT 'KG' | varchar NOT NULL | text NOT NULL DEFAULT 'KG' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.standard_prices | fk:sub_category_id | ABSENTE | →governance.sub_categories(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.standard_prices | fk:updated_by_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.standard_prices | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.sub_categories | col:allowed_units | text[] NULL | varchar[] NULL | text[] NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.sub_categories | col:blocked_zone_ids | text[] NOT NULL DEFAULT '{}' | varchar[] NOT NULL DEFAULT '{}' | text[] NOT NULL DEFAULT '{}' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.sub_categories | col:minimum_order_unit | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.sub_categories | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.sub_categories | col:priority_unit | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.sub_categories | fk:category_id | ABSENTE | →governance.categories(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.user_organizations | col:role | text NOT NULL DEFAULT 'FIELD_AGENT' | varchar NOT NULL | text NOT NULL DEFAULT 'FIELD_AGENT' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.user_organizations | fk:managed_zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.user_organizations | fk:organization_id | ABSENTE | →governance.organizations(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.user_organizations | fk:role_id | ABSENTE | →governance.role_definitions(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.user_organizations | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.user_organizations | index:role_id | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| governance.user_organizations | index:user_id | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| governance.work_zones | col:role | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.work_zones | fk:manager_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.work_zones | fk:organization_id | ABSENTE | →governance.organizations(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.work_zones | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.zone_metrics | col:metric_name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.zone_metrics | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.zone_settings | col:key | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.zone_settings | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.zones | col:code | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.zones | col:depth | int4 NOT NULL DEFAULT 0 | int4 NOT NULL | int4 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| governance.zones | col:is_active | bool NOT NULL DEFAULT true | bool NOT NULL | bool NOT NULL DEFAULT true | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| governance.zones | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.zones | col:path | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| governance.zones | fk:climatic_region_id | ABSENTE | →governance.climatic_regions(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.zones | fk:organization_id | ABSENTE | →governance.organizations(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| governance.zones | fk:parent_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.agent_actions | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.agent_actions | col:priority | text NOT NULL DEFAULT 'MEDIUM' | varchar NOT NULL | text NOT NULL DEFAULT 'MEDIUM' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.agent_actions | col:status | text NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | text NOT NULL DEFAULT 'PENDING' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.agent_actions | fk:order_id | ABSENTE | →marketplace.orders(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.agent_actions | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.agent_context_memory | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.agent_context_memory | col:source | text NOT NULL DEFAULT 'AGENT' | text NOT NULL | text NOT NULL DEFAULT 'AGENT' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.agent_context_memory | fk:market_offer_id | ABSENTE | →marketplace.market_offers(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.agent_context_memory | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.ai_rating_reasonings | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.ai_rating_reasonings | fk:trust_score_id | ABSENTE | →intelligence.trust_scores(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.audit_logs | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.audit_logs | fk:actor_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.conversations | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.conversations | col:is_waiting_for_input | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.conversations | col:mode | text NOT NULL DEFAULT 'text' | text NOT NULL | text NOT NULL DEFAULT 'text' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.conversations | col:needs_follow_up | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.conversations | col:total_tokens_used | int4 NOT NULL DEFAULT 0 | int4 NOT NULL | int4 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.conversations | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.conversations | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| intelligence.demand_signals | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.moderation_events | col:action_taken | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.moderation_events | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.moderation_events | col:kind | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.notification_outbox | col:channel | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.notification_outbox | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.notification_outbox | col:status | text NOT NULL DEFAULT 'PENDING' | varchar NOT NULL DEFAULT 'PENDING' | text NOT NULL DEFAULT 'PENDING' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.notification_outbox | col:template_key | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.solicitations | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.solicitations | col:kind | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.solicitations | col:status | text NOT NULL DEFAULT 'PENDING' | varchar NOT NULL DEFAULT 'PENDING' | text NOT NULL DEFAULT 'PENDING' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| intelligence.trust_scores | col:compliance_index | float8 NOT NULL DEFAULT 0 | float8 NOT NULL | float8 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.trust_scores | col:global_score | float8 NOT NULL DEFAULT 0 | float8 NOT NULL | float8 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.trust_scores | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.trust_scores | col:quality_index | float8 NOT NULL DEFAULT 0 | float8 NOT NULL | float8 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.trust_scores | col:reliability_index | float8 NOT NULL DEFAULT 0 | float8 NOT NULL | float8 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.trust_scores | col:resilience_bonus | float8 NOT NULL DEFAULT 0 | float8 NOT NULL | float8 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| intelligence.trust_scores | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.auctions | col:auto_extend | bool NOT NULL DEFAULT true | bool NOT NULL | bool NOT NULL DEFAULT true | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.auctions | col:cancellation_reason | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:delivery_location | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:escrow_status | text NOT NULL DEFAULT 'NONE' | varchar NOT NULL | text NOT NULL DEFAULT 'NONE' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.auctions | col:images | text[] NOT NULL DEFAULT '{}' | varchar[] NOT NULL DEFAULT '{}' | text[] NOT NULL DEFAULT '{}' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:incoterm | text NOT NULL DEFAULT 'DDP' | varchar NOT NULL | text NOT NULL DEFAULT 'DDP' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:preferred_packaging | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:quality_grading | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:required_certifications | text[] NOT NULL DEFAULT '{}' | varchar[] NOT NULL DEFAULT '{}' | text[] NOT NULL DEFAULT '{}' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:status | text NOT NULL DEFAULT 'OPEN' | varchar NOT NULL | text NOT NULL DEFAULT 'OPEN' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:unit | text NOT NULL DEFAULT 'TONNE' | varchar NOT NULL | text NOT NULL DEFAULT 'TONNE' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.auctions | col:version | int4 NOT NULL DEFAULT 0 | int4 NOT NULL | int4 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.auctions | fk:buyer_id | ABSENTE | →marketplace.buyer_profiles(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.auctions | fk:sub_category_id | ABSENTE | →governance.sub_categories(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.auctions | fk:target_zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.auctions | fk:winner_bid_id | ABSENTE | →marketplace.bids(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.batches | col:batch_number | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.batches | fk:organization_id | ABSENTE | →governance.organizations(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.batches | fk:origin_farm_id | ABSENTE | →marketplace.farms(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.batches | fk:stock_id | ABSENTE | →marketplace.stocks(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.bids | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.bids | col:images | text[] NOT NULL DEFAULT '{}' | varchar[] NOT NULL DEFAULT '{}' | text[] NOT NULL DEFAULT '{}' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.bids | col:is_winner | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.bids | col:status | text NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | text NOT NULL DEFAULT 'PENDING' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.bids | fk:auction_id | ABSENTE | →marketplace.auctions(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.bids | fk:linked_stock_id | ABSENTE | →marketplace.stocks(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.bids | fk:producer_id | ABSENTE | →marketplace.producers(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.buyer_profiles | col:company_registration_number | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.buyer_profiles | col:establishment_name | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.buyer_profiles | col:is_verified | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.buyer_profiles | col:reviews_count | int4 NOT NULL DEFAULT 0 | int4 NOT NULL | int4 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.buyer_profiles | col:trust_badge | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.buyer_profiles | fk:buyer_type_id | ABSENTE | →marketplace.buyer_types(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.buyer_profiles | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.buyer_types | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.clients | col:email | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.clients | col:location | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.clients | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.clients | col:phone | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.clients | col:tax_id | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.clients | col:total_orders | int4 NOT NULL DEFAULT 0 | int4 NOT NULL | int4 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.clients | col:total_spent | float8 NOT NULL DEFAULT 0 | float8 NOT NULL | float8 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.clients | fk:producer_id | ABSENTE | →marketplace.producers(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.deliveries | col:delivery_code | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.deliveries | col:proof_of_delivery_url | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.deliveries | col:shipping_condition | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.deliveries | col:status | text NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | text NOT NULL DEFAULT 'PENDING' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.deliveries | fk:delivery_agent_id | ABSENTE | →marketplace.delivery_agents(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.deliveries | fk:order_id | ABSENTE | →marketplace.orders(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.delivery_agents | col:license_number | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.delivery_agents | col:status | text NOT NULL DEFAULT 'OFFLINE' | varchar NOT NULL | text NOT NULL DEFAULT 'OFFLINE' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.delivery_agents | col:vehicle_type | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.delivery_agents | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.delivery_agents | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.delivery_agents | index:status | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| marketplace.delivery_agents | index:zone_id | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| marketplace.expenses | col:category | text NOT NULL DEFAULT 'OTHER' | varchar NOT NULL DEFAULT other | text NOT NULL DEFAULT 'OTHER' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.expenses | col:label | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.expenses | fk:farm_id | ABSENTE | →marketplace.farms(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.farms | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.farms | col:location | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.farms | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.market_offers | col:available_quantity | numeric(14,3) NOT NULL DEFAULT '0' | numeric(14,3) NOT NULL | numeric(14,3) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.market_offers | col:breed | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.market_offers | col:current_stock | numeric(14,3) NOT NULL DEFAULT '0' | numeric(14,3) NOT NULL | numeric(14,3) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.market_offers | col:is_public | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.market_offers | col:preorder_enabled | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.market_offers | col:product_label | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.market_offers | col:production_type | text NOT NULL DEFAULT 'CROP' | varchar NOT NULL | text NOT NULL DEFAULT 'CROP' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.market_offers | col:reserved_quantity | numeric(14,3) NOT NULL DEFAULT '0' | numeric(14,3) NOT NULL | numeric(14,3) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.market_offers | col:species | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.market_offers | col:status | text NOT NULL DEFAULT 'DRAFT' | varchar NOT NULL | text NOT NULL DEFAULT 'DRAFT' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.market_offers | col:unit | text NOT NULL DEFAULT 'KG' | varchar NOT NULL | text NOT NULL DEFAULT 'KG' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.market_offers | fk:sub_category_id | ABSENTE | →governance.sub_categories(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.marketplace_ratings | col:author_type | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.marketplace_ratings | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.marketplace_ratings | col:target_type | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.order_disputes | col:disputed_amount | numeric(14,2) NOT NULL DEFAULT '0' | numeric(14,2) NOT NULL | numeric(14,2) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_disputes | col:escrow_payout_status | varchar NOT NULL DEFAULT 'HELD' | varchar NOT NULL | varchar NOT NULL DEFAULT 'HELD' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_disputes | col:evidence_images | text[] NOT NULL DEFAULT '{}' | varchar[] NOT NULL DEFAULT '{}' | text[] NOT NULL DEFAULT '{}' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.order_disputes | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_disputes | col:status | varchar NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | varchar NOT NULL DEFAULT 'PENDING' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_items | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_items | col:tier_id | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.order_reminders | col:attempts | int4 NOT NULL DEFAULT 0 | int4 NOT NULL | int4 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_reminders | col:channel | varchar NOT NULL DEFAULT 'WHATSAPP' | varchar NOT NULL | varchar NOT NULL DEFAULT 'WHATSAPP' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_reminders | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_reminders | col:status | varchar NOT NULL DEFAULT 'SCHEDULED' | varchar NOT NULL | varchar NOT NULL DEFAULT 'SCHEDULED' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.order_status_history | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:currency | varchar NOT NULL DEFAULT 'XOF' | varchar NOT NULL | varchar NOT NULL DEFAULT 'XOF' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:delivery_fee | numeric(14,2) NOT NULL DEFAULT '0' | numeric(14,2) NOT NULL | numeric(14,2) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:delivery_otp | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.orders | col:delivery_status | varchar NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | varchar NOT NULL DEFAULT 'PENDING' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:is_agent_order | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:order_type | varchar NOT NULL DEFAULT 'STANDARD' | varchar NOT NULL | varchar NOT NULL DEFAULT 'STANDARD' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:paydunya_invoice_token | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.orders | col:payment_method | varchar NOT NULL DEFAULT 'CASH' | varchar NOT NULL | varchar NOT NULL DEFAULT 'CASH' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:payment_status | varchar NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | varchar NOT NULL DEFAULT 'PENDING' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:source | varchar NOT NULL DEFAULT 'APP' | varchar NOT NULL | varchar NOT NULL DEFAULT 'APP' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:status | varchar NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | varchar NOT NULL DEFAULT 'PENDING' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:subtotal | numeric(14,2) NOT NULL DEFAULT '0' | numeric(14,2) NOT NULL | numeric(14,2) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.orders | col:tax_amount | numeric(14,2) NOT NULL DEFAULT '0' | numeric(14,2) NOT NULL | numeric(14,2) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.payments | col:currency | varchar NOT NULL DEFAULT 'XOF' | varchar NOT NULL | varchar NOT NULL DEFAULT 'XOF' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.payments | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.payments | col:method | varchar NOT NULL DEFAULT 'CASH' | varchar NOT NULL | varchar NOT NULL DEFAULT 'CASH' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.payments | col:status | varchar NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | varchar NOT NULL DEFAULT 'PENDING' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.producers | col:business_name | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | col:commune | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | col:company_registration_number | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | col:is_certified | bool NOT NULL DEFAULT false | bool NOT NULL | bool NOT NULL DEFAULT false | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.producers | col:logo_url | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | col:phone_number | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | col:province | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | col:region | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | col:reviews_count | int4 NOT NULL DEFAULT 0 | int4 NOT NULL | int4 NOT NULL DEFAULT 0 | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.producers | col:status | text NOT NULL DEFAULT 'PENDING' | varchar NOT NULL | text NOT NULL DEFAULT 'PENDING' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.producers | fk:organization_id | ABSENTE | →governance.organizations(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.producers | fk:user_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.producers | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.products | col:audio_url | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:category_label | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:id | uuid NOT NULL DEFAULT gen_random_uuid() | uuid NOT NULL | uuid NOT NULL DEFAULT gen_random_uuid() | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.products | col:images | text[] NOT NULL DEFAULT '{}' | varchar[] NOT NULL DEFAULT '{}' | text[] NOT NULL DEFAULT '{}' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:is_available | bool NOT NULL DEFAULT true | bool NOT NULL | bool NOT NULL DEFAULT true | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.products | col:min_order_quality | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:name | text NOT NULL DEFAULT 'Produit' | varchar NOT NULL | text NOT NULL DEFAULT 'Produit' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:packaging_type | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:quality_class | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:quantity_for_sale | numeric(14,3) NOT NULL DEFAULT '0' | numeric(14,3) NOT NULL | numeric(14,3) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.products | col:short_code | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | col:unit | text NOT NULL DEFAULT 'KG' | varchar NOT NULL | text NOT NULL DEFAULT 'KG' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.products | fk:sub_category_id | ABSENTE | →governance.sub_categories(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.products | fk:verified_by_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.seed_allocations | (table) | présente | ABSENTE | présente | DIVERGENT | Table Drizzle sans modèle Python : OK si non utilisée par Python (seed/site), sinon ajouter le miroir  [D=✓ S=✗ P=✓] |
| marketplace.seed_distribution_attempts | (table) | présente | ABSENTE | présente | DIVERGENT | Table Drizzle sans modèle Python : OK si non utilisée par Python (seed/site), sinon ajouter le miroir  [D=✓ S=✗ P=✓] |
| marketplace.seed_distributions | (table) | présente | ABSENTE | présente | DIVERGENT | Table Drizzle sans modèle Python : OK si non utilisée par Python (seed/site), sinon ajouter le miroir  [D=✓ S=✗ P=✓] |
| marketplace.stock_movements | col:reason | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.stock_movements | col:type | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.stock_movements | fk:stock_id | ABSENTE | →marketplace.stocks(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.stocks | col:item_name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.stocks | col:quantity | numeric(14,3) NOT NULL DEFAULT '0' | numeric(14,3) NOT NULL | numeric(14,3) NOT NULL DEFAULT '0' | DIVERGENT | Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile) |
| marketplace.stocks | col:type | text NOT NULL DEFAULT 'HARVEST' | varchar NOT NULL | text NOT NULL DEFAULT 'HARVEST' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.stocks | col:unit | text NOT NULL DEFAULT 'KG' | varchar NOT NULL | text NOT NULL DEFAULT 'KG' | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.stocks | fk:farm_id | ABSENTE | →marketplace.farms(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.stocks | fk:organization_id | ABSENTE | →governance.organizations(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.stocks | fk:verified_by_id | ABSENTE | →auth.users(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.stocks | fk:warehouse_id | ABSENTE | →marketplace.warehouses(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| marketplace.stocks | index:organization_id | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| marketplace.stocks | index:type | oui | ABSENT | oui | DIVERGENT | Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête |
| marketplace.warehouses | col:location | text NULL | varchar NULL | text NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.warehouses | col:name | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.warehouses | col:type | text NOT NULL | varchar NOT NULL | text NOT NULL | DIVERGENT | Aligner le type sur Drizzle (source de vérité) |
| marketplace.warehouses | fk:zone_id | ABSENTE | →governance.zones(id) del=NO ACTION upd=NO ACTION | ABSENTE | DIVERGENT | Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer |
| public.episodic_memories | (table) | ABSENTE | présente | ABSENTE | DIVERGENT | Modèle sans table Drizzle : supprimer le modèle (mort) OU déclarer la table dans Drizzle  [D=✗ S=✓ P=✗] |
| public.user_farm_profiles | (table) | ABSENTE | présente | ABSENTE | DIVERGENT | Modèle sans table Drizzle : supprimer le modèle (mort) OU déclarer la table dans Drizzle  [D=✗ S=✓ P=✗] |
