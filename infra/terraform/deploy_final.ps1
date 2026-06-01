param(
    [string]$Region = "eu-north-1",
    [string]$AccountId = "875180007527",
    [string]$ImageTag = "latest",
    [string]$ScraperServiceName = "agriconnect-scrapers-service",
    [string]$IngestionServiceName = "agriconnect-ingestion-service",
    [string]$ClusterName = "agriconnect-cluster",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$ScraperRepo = "$($AccountId).dkr.ecr.$Region.amazonaws.com/agriconnect-scrapers-dev"
$IngestionRepo = "$($AccountId).dkr.ecr.$Region.amazonaws.com/agriconnect-ingestion-dev"

Write-Host "Region: $Region"; Write-Host "Account: $AccountId"; Write-Host "ImageTag: $ImageTag"

if ($DryRun) { Write-Host "DRY RUN - no push or ECS update will be executed" }

function ExecOrShow($cmd) {
    if ($DryRun) { Write-Host "DRY: $cmd" } else { Invoke-Expression $cmd }
}

Write-Host "Logging into ECR..."
ExecOrShow "aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $($AccountId).dkr.ecr.$Region.amazonaws.com"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\.." )).Path
$ScraperContext = $RepoRoot
$IngestionContext = $RepoRoot

Write-Host "Building scraper image..."
ExecOrShow "docker build -f infra/docker/Dockerfile.scraper -t agriconnect-scrapers:$ImageTag `"$ScraperContext`""
ExecOrShow "docker tag agriconnect-scrapers:$ImageTag ${ScraperRepo}:$ImageTag"

Write-Host "Building ingestion image..."
ExecOrShow "docker build -f infra/docker/Dockerfile.ingestion -t agriconnect-ingestion:$ImageTag `"$IngestionContext`""
ExecOrShow "docker tag agriconnect-ingestion:$ImageTag ${IngestionRepo}:$ImageTag"

Write-Host "Pushing images to ECR..."
ExecOrShow "docker push ${ScraperRepo}:$ImageTag"
ExecOrShow "docker push ${IngestionRepo}:$ImageTag"

Write-Host "Forcing new deployment on ECS services"
ExecOrShow "aws ecs update-service --cluster $ClusterName --service $ScraperServiceName --force-new-deployment --region $Region"
ExecOrShow "aws ecs update-service --cluster $ClusterName --service $IngestionServiceName --force-new-deployment --region $Region"

Write-Host "Done. If not DryRun, monitor tasks and logs in CloudWatch."
