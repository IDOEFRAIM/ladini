data "aws_caller_identity" "current" {}

resource "aws_sqs_queue" "pdf_links_dlq" {
  name                      = var.pdf_links_dlq_name
  message_retention_seconds = 1209600
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "pdf_links" {
  name                       = var.pdf_links_queue_name
  visibility_timeout_seconds = 180
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.pdf_links_dlq.arn
    maxReceiveCount     = 5
  })
  tags = local.common_tags
}

resource "aws_sqs_queue" "ingestion_dlq" {
  name                      = var.ingestion_dlq_name
  message_retention_seconds = 1209600
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "ingestion" {
  name                       = var.ingestion_queue_name
  visibility_timeout_seconds = 180
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingestion_dlq.arn
    maxReceiveCount     = 5
  })
  tags = local.common_tags
}

data "aws_iam_policy_document" "s3_to_ingestion_queue" {
  statement {
    sid    = "AllowS3ToSendObjectCreated"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingestion.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.raw.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "ingestion_queue_policy" {
  queue_url = aws_sqs_queue.ingestion.id
  policy    = data.aws_iam_policy_document.s3_to_ingestion_queue.json
}
