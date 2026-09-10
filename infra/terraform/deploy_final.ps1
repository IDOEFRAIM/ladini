param(
    [string]$Region = "eu-north-1",
    [string]$AccountId = "875180007527",
    [string]$ImageTag = "latest",
    [string]$ScraperServiceName = "ladini-scrapers-service",
    [string]$IngestionServiceName = "ladini-ingestion-service",
    [string]$ClusterName = "ladini-cluster",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$ScraperRepo = "$($AccountId).dkr.ecr.$Region.amazonaws.com/ladini-scrapers-dev"
$IngestionRepo = "$($AccountId).dkr.ecr.$Region.amazonaws.com/ladini-ingestion-dev"

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
ExecOrShow "docker build -f infra/docker/Dockerfile.scraper -t ladini-scrapers:$ImageTag `"$ScraperContext`""
ExecOrShow "docker tag ladini-scrapers:$ImageTag ${ScraperRepo}:$ImageTag"

Write-Host "Building ingestion image..."
ExecOrShow "docker build -f infra/docker/Dockerfile.ingestion -t ladini-ingestion:$ImageTag `"$IngestionContext`""
ExecOrShow "docker tag ladini-ingestion:$ImageTag ${IngestionRepo}:$ImageTag"

Write-Host "Pushing images to ECR..."
ExecOrShow "docker push ${ScraperRepo}:$ImageTag"
ExecOrShow "docker push ${IngestionRepo}:$ImageTag"

Write-Host "Forcing new deployment on ECS services"
ExecOrShow "aws ecs update-service --cluster $ClusterName --service $ScraperServiceName --force-new-deployment --region $Region"
ExecOrShow "aws ecs update-service --cluster $ClusterName --service $IngestionServiceName --force-new-deployment --region $Region"

Write-Host "Done. If not DryRun, monitor tasks and logs in CloudWatch."
