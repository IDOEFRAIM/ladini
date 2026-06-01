data "archive_file" "downloader_zip" {
  type        = "zip"
  source_dir  = var.downloader_lambda_source_dir
  output_path = "${path.module}/downloader_lambda.zip"
}

resource "aws_cloudwatch_log_group" "downloader_lambda" {
  name              = "/aws/lambda/${var.downloader_lambda_function_name}-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_lambda_function" "downloader" {
  function_name    = "${var.downloader_lambda_function_name}-${var.environment}"
  role             = aws_iam_role.downloader_lambda_role.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = var.downloader_lambda_timeout
  memory_size      = var.downloader_lambda_memory_size
  filename         = data.archive_file.downloader_zip.output_path
  source_code_hash = data.archive_file.downloader_zip.output_base64sha256

  environment {
    variables = {
      PDF_BUCKET = aws_s3_bucket.raw.id
    }
  }

  tags = local.common_tags
}

resource "aws_lambda_event_source_mapping" "downloader_from_links_queue" {
  event_source_arn = aws_sqs_queue.pdf_links.arn
  function_name    = aws_lambda_function.downloader.arn
  batch_size       = var.downloader_batch_size
  enabled          = true
}
