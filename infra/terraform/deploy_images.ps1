param(
    [string]$Region = "eu-west-3",
    [string]$ImageTag = "latest",
    [string]$ClusterName = "ladini-cluster-dev",
    [string]$ScraperServiceName = "ladini-scraper-service-dev",
    [string]$IngestionServiceName = "ladini-ingestion-service-dev",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    Write-Host "==> $Description" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host "[DRY-RUN] Etape ignorée." -ForegroundColor Yellow
        return
    }
    & $Action
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DockerDir = (Resolve-Path (Join-Path $ScriptDir "..\docker")).Path

Set-Location $ScriptDir

Invoke-Step -Description "Lecture des outputs Terraform ECR" -Action {
    $script:ScraperRepo = terraform output -raw scrapers_ecr_repository_url
    $script:IngestionRepo = terraform output -raw ingestion_ecr_repository_url
}

if (-not $DryRun) {
    if (-not $ScraperRepo -or -not $IngestionRepo) {
        throw "Impossible de lire les outputs Terraform des repositories ECR."
    }
}

if ($DryRun) {
    $ScraperRepo = "875180007527.dkr.ecr.$Region.amazonaws.com/ladini-scrapers-dev"
    $IngestionRepo = "875180007527.dkr.ecr.$Region.amazonaws.com/ladini-ingestion-dev"
}

$Registry = ($ScraperRepo -split "/")[0]

Invoke-Step -Description "Login ECR ($Registry)" -Action {
    aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Registry
}

Invoke-Step -Description "Build image Scraper" -Action {
    docker build -f (Join-Path $DockerDir "Dockerfile.scraper") -t "ladini-scraper-local:$ImageTag" $RepoRoot
}

Invoke-Step -Description "Build image Ingestion" -Action {
    docker build -f (Join-Path $DockerDir "Dockerfile.ingestion") -t "ladini-ingestion-local:$ImageTag" $RepoRoot
}

Invoke-Step -Description "Tag images for ECR" -Action {
    docker tag "ladini-scraper-local:$ImageTag" "${ScraperRepo}:$ImageTag"
    docker tag "ladini-ingestion-local:$ImageTag" "${IngestionRepo}:$ImageTag"
}

Invoke-Step -Description "Push Scraper image" -Action {
    docker push "${ScraperRepo}:$ImageTag"
}

Invoke-Step -Description "Push Ingestion image" -Action {
    docker push "${IngestionRepo}:$ImageTag"
}

Invoke-Step -Description "Force ECS redeploy Scraper service" -Action {
    aws ecs update-service --region $Region --cluster $ClusterName --service $ScraperServiceName --force-new-deployment | Out-Null
}

Invoke-Step -Description "Force ECS redeploy Ingestion service" -Action {
    aws ecs update-service --region $Region --cluster $ClusterName --service $IngestionServiceName --force-new-deployment | Out-Null
}

Write-Host "Deploiement termine." -ForegroundColor Green
Write-Host "Scraper image: ${ScraperRepo}:$ImageTag"
Write-Host "Ingestion image: ${IngestionRepo}:$ImageTag"
