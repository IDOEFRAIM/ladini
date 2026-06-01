resource "aws_ecr_repository" "scrapers" {
  name = "${var.scrapers_ecr_repo_name}-${var.environment}"
  tags = local.common_tags
}

resource "aws_ecr_repository" "ingestion" {
  name = "${var.ingestion_ecr_repo_name}-${var.environment}"
  tags = local.common_tags
}
