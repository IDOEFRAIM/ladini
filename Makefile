# Makefile - Agribot-AI Cloud-Native Deployment
# Simplifie toutes les opérations de développement et déploiement

.PHONY: help install test lint build-local deploy-local stop-local clean \
        build-images push-images deploy-k8s rollback monitoring

# Variables
REGISTRY ?= ghcr.io/your-org/agribot
VERSION ?= latest
CLOUD ?= aws
REGION ?= eu-west-1
CLUSTER ?= agribot-prod

# Colors for output
GREEN  := \033[0;32m
YELLOW := \033[0;33m
RED    := \033[0;31m
NC     := \033[0m # No Color

##@ Help
help: ## Display this help
	@echo "$(GREEN)Agribot-AI Cloud-Native Deployment$(NC)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make $(YELLOW)<target>$(NC)\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2 } /^##@/ { printf "\n$(YELLOW)%s$(NC)\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Development
install: ## Install dependencies
	@echo "$(GREEN)Installing dependencies...$(NC)"
	pip install -r requirements_agrios.txt
	pip install pytest ruff mypy

run: ## Run the backend server
	@echo "$(GREEN)Starting backend server...$(NC)"
	uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

test: ## Run all tests
	@echo "$(GREEN)Running tests...$(NC)"
	pytest tests/ -v --cov=backend --cov-report=term-missing

lint: ## Run linter (Ruff)
	@echo "$(GREEN)Linting code...$(NC)"
	ruff check backend/

typecheck: ## Run type checker (mypy)
	@echo "$(GREEN)Type checking...$(NC)"
	mypy backend/ --ignore-missing-imports

migrate: ## Run database migrations
	@echo "$(GREEN)Running database migrations...$(NC)"
	python backend/scripts/core/seed_db.py

seed: ## Seed the database with initial data
	@echo "$(GREEN)Seeding database...$(NC)"
	python backend/scripts/core/seed_zones.py

##@ Local Development (Docker Compose)
build-local: ## Build Docker images locally
	@echo "$(GREEN)Building Docker images...$(NC)"
	docker-compose build

deploy-local: ## Deploy all services locally with Docker Compose
	@echo "$(GREEN)Starting services with Docker Compose...$(NC)"
	docker-compose up -d
	@echo "$(GREEN)Waiting for services to be ready...$(NC)"
	@sleep 10
	@echo "$(GREEN)Services started!$(NC)"
	@echo "Gateway:    http://localhost:8000"
	@echo "Database:   http://localhost:8003"
	@echo "Voice:      http://localhost:8002"
	@echo "Prometheus: http://localhost:9090"
	@echo "Grafana:    http://localhost:3000 (admin/admin_change_in_prod)"

logs-local: ## Show logs from all services
	docker-compose logs -f

stop-local: ## Stop local services
	@echo "$(YELLOW)Stopping services...$(NC)"
	docker-compose down

clean-local: ## Stop and remove all containers, volumes
	@echo "$(RED)Cleaning all containers and volumes...$(NC)"
	docker-compose down -v
	docker system prune -f

health-check: ## Check health of all services
	@echo "$(GREEN)Checking service health...$(NC)"
	@curl -s http://localhost:8000/health | jq . || echo "$(RED)Gateway not responding$(NC)"
	@curl -s http://localhost:8003/health | jq . || echo "$(RED)Database not responding$(NC)"
	@curl -s http://localhost:8002/health | jq . || echo "$(RED)Voice not responding$(NC)"

##@ Docker Images
build-gateway: ## Build Gateway service image
	@echo "$(GREEN)Building Gateway image...$(NC)"
	docker build -t $(REGISTRY)-gateway:$(VERSION) services/gateway/

build-database: ## Build Database service image
	@echo "$(GREEN)Building Database image...$(NC)"
	docker build -t $(REGISTRY)-database:$(VERSION) services/database/

build-voice: ## Build Voice service image
	@echo "$(GREEN)Building Voice image...$(NC)"
	docker build -t $(REGISTRY)-voice:$(VERSION) services/voice/

build-images: build-gateway build-database build-voice ## Build all Docker images

push-gateway: ## Push Gateway image to registry
	docker push $(REGISTRY)-gateway:$(VERSION)

push-database: ## Push Database image to registry
	docker push $(REGISTRY)-database:$(VERSION)

push-voice: ## Push Voice image to registry
	docker push $(REGISTRY)-voice:$(VERSION)

push-images: push-gateway push-database push-voice ## Push all images to registry

##@ Kubernetes Deployment
k8s-context-aws: ## Set kubectl context for AWS EKS
	@echo "$(GREEN)Setting AWS EKS context...$(NC)"
	aws eks update-kubeconfig --name $(CLUSTER) --region $(REGION)

k8s-context-azure: ## Set kubectl context for Azure AKS
	@echo "$(GREEN)Setting Azure AKS context...$(NC)"
	az aks get-credentials --resource-group agribot-rg --name $(CLUSTER)

k8s-context-gcp: ## Set kubectl context for GCP GKE
	@echo "$(GREEN)Setting GCP GKE context...$(NC)"
	gcloud container clusters get-credentials $(CLUSTER) --zone $(REGION)

deploy-k8s-secrets: ## Deploy secrets to Kubernetes
	@echo "$(GREEN)Deploying secrets...$(NC)"
	kubectl apply -f k8s/config-secrets.yaml

deploy-k8s-redis: ## Deploy Redis to Kubernetes
	@echo "$(GREEN)Deploying Redis...$(NC)"
	kubectl apply -f k8s/redis-deployment.yaml

deploy-k8s-database: ## Deploy Database service to Kubernetes
	@echo "$(GREEN)Deploying Database service...$(NC)"
	kubectl apply -f k8s/database-deployment.yaml
	kubectl rollout status deployment/database-service --timeout=3m

deploy-k8s-voice: ## Deploy Voice service to Kubernetes
	@echo "$(GREEN)Deploying Voice service...$(NC)"
	kubectl apply -f k8s/voice-deployment.yaml
	kubectl rollout status deployment/voice-service --timeout=3m

deploy-k8s-gateway: ## Deploy Gateway service to Kubernetes
	@echo "$(GREEN)Deploying Gateway service...$(NC)"
	kubectl apply -f k8s/gateway-deployment.yaml
	kubectl rollout status deployment/gateway --timeout=3m

deploy-k8s-ingress: ## Deploy Ingress to Kubernetes
	@echo "$(GREEN)Deploying Ingress...$(NC)"
	kubectl apply -f k8s/ingress.yaml

deploy-k8s: deploy-k8s-secrets deploy-k8s-redis deploy-k8s-database deploy-k8s-voice deploy-k8s-gateway deploy-k8s-ingress ## Deploy all to Kubernetes
	@echo "$(GREEN)All services deployed!$(NC)"
	@kubectl get pods
	@kubectl get services
	@kubectl get ingress

k8s-status: ## Check Kubernetes deployment status
	@echo "$(GREEN)Pods:$(NC)"
	@kubectl get pods
	@echo "\n$(GREEN)Services:$(NC)"
	@kubectl get services
	@echo "\n$(GREEN)Ingress:$(NC)"
	@kubectl get ingress
	@echo "\n$(GREEN)HPA:$(NC)"
	@kubectl get hpa

k8s-logs-gateway: ## Show Gateway logs
	kubectl logs -f deployment/gateway --tail=100

k8s-logs-database: ## Show Database logs
	kubectl logs -f deployment/database-service --tail=100

k8s-logs-voice: ## Show Voice logs
	kubectl logs -f deployment/voice-service --tail=100

k8s-shell-gateway: ## Open shell in Gateway pod
	kubectl exec -it deployment/gateway -- /bin/sh

rollback-gateway: ## Rollback Gateway to previous version
	@echo "$(YELLOW)Rolling back Gateway...$(NC)"
	kubectl rollout undo deployment/gateway

rollback-database: ## Rollback Database to previous version
	@echo "$(YELLOW)Rolling back Database...$(NC)"
	kubectl rollout undo deployment/database-service

rollback-voice: ## Rollback Voice to previous version
	@echo "$(YELLOW)Rolling back Voice...$(NC)"
	kubectl rollout undo deployment/voice-service

##@ Monitoring
monitoring-install: ## Install Prometheus + Grafana with Helm
	@echo "$(GREEN)Installing Prometheus + Grafana...$(NC)"
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update
	helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
		--namespace monitoring --create-namespace

monitoring-port-forward: ## Port-forward Grafana to localhost:3000
	@echo "$(GREEN)Forwarding Grafana to localhost:3000$(NC)"
	kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80

prometheus-port-forward: ## Port-forward Prometheus to localhost:9090
	@echo "$(GREEN)Forwarding Prometheus to localhost:9090$(NC)"
	kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090

##@ AWS Specific
aws-create-cluster: ## Create AWS EKS cluster
	@echo "$(GREEN)Creating EKS cluster...$(NC)"
	eksctl create cluster \
		--name $(CLUSTER) \
		--region $(REGION) \
		--nodegroup-name standard-workers \
		--node-type t3.medium \
		--nodes 3 \
		--nodes-min 2 \
		--nodes-max 10 \
		--managed

aws-delete-cluster: ## Delete AWS EKS cluster
	@echo "$(RED)Deleting EKS cluster...$(NC)"
	eksctl delete cluster --name $(CLUSTER) --region $(REGION)

aws-ecr-login: ## Login to AWS ECR
	@echo "$(GREEN)Logging into AWS ECR...$(NC)"
	aws ecr get-login-password --region $(REGION) | \
		docker login --username AWS --password-stdin $$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$(REGION).amazonaws.com

##@ Azure Specific
azure-create-cluster: ## Create Azure AKS cluster
	@echo "$(GREEN)Creating AKS cluster...$(NC)"
	az aks create \
		--resource-group agribot-rg \
		--name $(CLUSTER) \
		--node-count 3 \
		--node-vm-size Standard_D2s_v3 \
		--enable-managed-identity \
		--generate-ssh-keys

azure-delete-cluster: ## Delete Azure AKS cluster
	@echo "$(RED)Deleting AKS cluster...$(NC)"
	az aks delete --resource-group agribot-rg --name $(CLUSTER) --yes

##@ GCP Specific
gcp-create-cluster: ## Create GCP GKE cluster
	@echo "$(GREEN)Creating GKE cluster...$(NC)"
	gcloud container clusters create $(CLUSTER) \
		--zone $(REGION) \
		--num-nodes 3 \
		--machine-type n1-standard-2 \
		--enable-autoscaling \
		--min-nodes 2 \
		--max-nodes 10

gcp-delete-cluster: ## Delete GCP GKE cluster
	@echo "$(RED)Deleting GKE cluster...$(NC)"
	gcloud container clusters delete $(CLUSTER) --zone $(REGION) --quiet

##@ Cleanup
clean: ## Clean all local Docker resources
	@echo "$(YELLOW)Cleaning Docker resources...$(NC)"
	docker system prune -af --volumes

k8s-delete: ## Delete all Kubernetes resources
	@echo "$(RED)Deleting Kubernetes resources...$(NC)"
	kubectl delete -f k8s/

##@ Quick Commands
dev: deploy-local ## Quick start for development
	@echo "$(GREEN)Development environment ready!$(NC)"

prod-aws: k8s-context-aws build-images push-images deploy-k8s ## Full production deployment to AWS
	@echo "$(GREEN)Production deployment to AWS complete!$(NC)"

prod-azure: k8s-context-azure build-images push-images deploy-k8s ## Full production deployment to Azure
	@echo "$(GREEN)Production deployment to Azure complete!$(NC)"

prod-gcp: k8s-context-gcp build-images push-images deploy-k8s ## Full production deployment to GCP
	@echo "$(GREEN)Production deployment to GCP complete!$(NC)"

.DEFAULT_GOAL := help
