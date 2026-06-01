output "raw_bucket_name" {
  value = aws_s3_bucket.raw.id
}

output "pdf_links_queue_url" {
  value = aws_sqs_queue.pdf_links.id
}

output "pdf_links_dlq_url" {
  value = aws_sqs_queue.pdf_links_dlq.id
}

output "ingestion_queue_url" {
  value = aws_sqs_queue.ingestion.id
}

output "ingestion_dlq_url" {
  value = aws_sqs_queue.ingestion_dlq.id
}

output "scrapers_ecr_repository_url" {
  value = aws_ecr_repository.scrapers.repository_url
}

output "ingestion_ecr_repository_url" {
  value = aws_ecr_repository.ingestion.repository_url
}

output "downloader_lambda_name" {
  value = aws_lambda_function.downloader.function_name
}
