output "vpc_id" {
  description = "ID of the private AppStream VPC."
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "Private subnet IDs used by the AppStream fleet."
  value       = [for subnet in aws_subnet.private : subnet.id]
}

output "appstream_stack_name" {
  description = "AppStream stack name."
  value       = aws_appstream_stack.main.name
}

output "appstream_fleet_name" {
  description = "AppStream fleet name."
  value       = aws_appstream_fleet.main.name
}

output "aws_account_id" {
  description = "AWS account receiving the resources."
  value       = data.aws_caller_identity.current.account_id
}