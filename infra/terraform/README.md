# Terraform - Ladini Pipeline

Ce dossier déploie l'infrastructure AWS du pipeline Ladini :
- Scrapers (ECS Fargate)
- Downloader PDF (Lambda + SQS)
- Ingestion Worker (ECS Fargate)
- S3 Raw bucket + notifications S3 -> SQS
- Files SQS + DLQ
- Rôles IAM en moindre privilège
- Dépôts ECR (scrapers / ingestion)

## Fichiers clés

- `provider.tf` : provider AWS avec variables (`aws_access_key`, `aws_secret_key`, `aws_region`)
- `variables.tf` : toutes les variables d'entrée
- `terraform.tfvars.example` : exemple à copier en `terraform.tfvars`
- `iam.tf` : rôles IAM
- `sqs.tf` : queues + DLQs
- `s3.tf` : bucket raw + notification S3 -> queue ingestion
- `ecs.tf` : cluster, task definitions et services ECS
- `lambda.tf` : downloader Lambda + event source mapping sur la queue des liens
- `outputs.tf` : sorties de déploiement

## Injection sécurisée des accès AWS

### Option A (recommandée) - `terraform.tfvars`

1. Copier l'exemple :

```bash
cp terraform.tfvars.example terraform.tfvars
```

2. Remplir `terraform.tfvars` avec tes vraies valeurs (surtout clés AWS, VPC, subnets, secret DB).

`terraform.tfvars` est ignoré via `.gitignore` dans ce dossier.

### Option B - Variables d'environnement Terraform

Terraform lit automatiquement les variables préfixées `TF_VAR_`.

PowerShell :

```powershell
$env:TF_VAR_aws_access_key = "AKIA..."
$env:TF_VAR_aws_secret_key = "..."
$env:TF_VAR_aws_region = "eu-west-3"
$env:TF_VAR_vpc_id = "vpc-xxxxxxxx"
$env:TF_VAR_db_secret_arn = "arn:aws:secretsmanager:eu-west-3:123456789012:secret:ladini/db-xxxxx"
```

Pour `public_subnet_ids`, il est plus simple d'utiliser `terraform.tfvars`.

## Commandes CLI de déploiement

Depuis ce dossier :

```bash
terraform init
terraform fmt -recursive
terraform validate
terraform plan
terraform apply -auto-approve
```

## Pré-requis importants

- Les images ECR référencées par `image_tag` doivent exister avant le démarrage ECS.
- Le code Lambda est empaqueté automatiquement depuis :
  - `../../backend/src/ladini/services/scraper/scrapers/lambdas/pdf_downloader`
- Les services ECS sont configurés avec `assign_public_ip = true` et des subnets publics.

## Sécurité

- Tous les rôles IAM sont séparés par usage (execution/task/lambda/ingestion).
- Les permissions sont limitées aux ressources nécessaires du pipeline.
- Toutes les ressources taggables portent `Project = Ladini`.
