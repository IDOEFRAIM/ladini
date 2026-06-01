resource "aws_ecs_cluster" "agriconnect" {
  name = "agriconnect-cluster-${var.environment}"
  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "scraper" {
  name              = "/ecs/agriconnect-scraper-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "ingestion" {
  name              = "/ecs/agriconnect-ingestion-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_security_group" "ecs_tasks" {
  name        = "agriconnect-ecs-tasks-sg-${var.environment}"
  description = "Security group for Agriconnect ECS tasks"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.common_tags
}

resource "aws_ecs_task_definition" "scraper" {
  family                   = "agriconnect-scraper-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.scraper_task_cpu
  memory                   = var.scraper_task_memory
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.scraper_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "scraper"
      image     = "${aws_ecr_repository.scrapers.repository_url}:${var.image_tag}"
      essential = true
      environment = [
        { name = "SCRAPER_SQS_URL", value = aws_sqs_queue.pdf_links.id }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.scraper.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "scraper"
        }
      }
    }
  ])

  tags = local.common_tags
}

resource "aws_ecs_task_definition" "ingestion" {
  family                   = "agriconnect-ingestion-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.ingestion_task_cpu
  memory                   = var.ingestion_task_memory
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ingestion_worker_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "ingestion-worker"
      image     = "${aws_ecr_repository.ingestion.repository_url}:${var.image_tag}"
      essential = true
      environment = [
        { name = "INGESTION_SQS_URL", value = aws_sqs_queue.ingestion.id },
        { name = "INGESTION_S3_BUCKET", value = aws_s3_bucket.raw.id },
        { name = "DB_SECRET_ARN", value = var.db_secret_arn }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ingestion.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ingestion"
        }
      }
    }
  ])

  tags = local.common_tags
}

resource "aws_ecs_service" "scraper" {
  name            = "agriconnect-scraper-service-${var.environment}"
  cluster         = aws_ecs_cluster.agriconnect.id
  task_definition = aws_ecs_task_definition.scraper.arn
  desired_count   = var.scraper_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  tags = local.common_tags
}

resource "aws_ecs_service" "ingestion" {
  name            = "agriconnect-ingestion-service-${var.environment}"
  cluster         = aws_ecs_cluster.agriconnect.id
  task_definition = aws_ecs_task_definition.ingestion.arn
  desired_count   = var.ingestion_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  tags = local.common_tags
}
