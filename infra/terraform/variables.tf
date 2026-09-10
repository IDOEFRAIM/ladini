variable "aws_access_key" {
  description = "AWS access key (set via terraform.tfvars or TF_VAR_aws_access_key)"
  type        = string
  sensitive   = true
}

variable "aws_secret_key" {
  description = "AWS secret key (set via terraform.tfvars or TF_VAR_aws_secret_key)"
  type        = string
  sensitive   = true
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "eu-west-3"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"
}

variable "bucket_name" {
  description = "Raw ingestion S3 bucket name"
  type        = string
  default     = "ladini-ingestion-raw"
}

variable "pdf_links_queue_name" {
  description = "Queue receiving discovered PDF links"
  type        = string
  default     = "pdf-links-queue"
}

variable "pdf_links_dlq_name" {
  description = "DLQ for pdf-links queue"
  type        = string
  default     = "pdf-links-dlq"
}

variable "ingestion_queue_name" {
  description = "Queue receiving S3 ObjectCreated events"
  type        = string
  default     = "s3-events-queue"
}

variable "ingestion_dlq_name" {
  description = "DLQ for ingestion queue"
  type        = string
  default     = "s3-events-dlq"
}

variable "scrapers_ecr_repo_name" {
  description = "ECR repository name for scrapers/downloader image"
  type        = string
  default     = "ladini-scrapers"
}

variable "ingestion_ecr_repo_name" {
  description = "ECR repository name for ingestion image"
  type        = string
  default     = "ladini-ingestion"
}

variable "image_tag" {
  description = "Container image tag used by ECS services"
  type        = string
  default     = "latest"
}

variable "vpc_id" {
  description = "VPC ID for ECS services"
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs used by ECS services"
  type        = list(string)
}

variable "scraper_desired_count" {
  description = "Number of scraper tasks"
  type        = number
  default     = 1
}

variable "ingestion_desired_count" {
  description = "Number of ingestion worker tasks"
  type        = number
  default     = 1
}

variable "scraper_task_cpu" {
  description = "Fargate CPU for scraper task"
  type        = string
  default     = "512"
}

variable "scraper_task_memory" {
  description = "Fargate memory for scraper task"
  type        = string
  default     = "1024"
}

variable "ingestion_task_cpu" {
  description = "Fargate CPU for ingestion task"
  type        = string
  default     = "512"
}

variable "ingestion_task_memory" {
  description = "Fargate memory for ingestion task"
  type        = string
  default     = "1024"
}

variable "db_secret_arn" {
  description = "Secrets Manager ARN containing DB credentials"
  type        = string
}

variable "downloader_lambda_function_name" {
  description = "Lambda function name for PDF downloader"
  type        = string
  default     = "ladini-pdf-downloader"
}

variable "downloader_lambda_source_dir" {
  description = "Folder containing downloader lambda source code"
  type        = string
  default     = "../../backend/src/ladini/services/scraper/scrapers/lambdas/pdf_downloader"
}

variable "downloader_lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 120
}

variable "downloader_lambda_memory_size" {
  description = "Lambda memory in MB"
  type        = number
  default     = 512
}

variable "downloader_batch_size" {
  description = "SQS batch size for downloader Lambda event source mapping"
  type        = number
  default     = 10
}

variable "log_retention_days" {
  description = "CloudWatch log retention period"
  type        = number
  default     = 14
}
