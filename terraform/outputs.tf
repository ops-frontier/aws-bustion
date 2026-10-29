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

output "appstream_login_url" {
  description = "Shared GitHub login URL for Linux Applications and Windows Personal."
  value       = "${aws_apigatewayv2_api.github_auth.api_endpoint}/login"
}

output "workspaces_directory_id" {
  description = "Managed Microsoft AD directory registered with WorkSpaces Personal."
  value       = aws_workspaces_directory.windows.id
}

output "workspaces_registration_code" {
  description = "WorkSpaces Personal client registration code."
  value       = aws_workspaces_directory.windows.registration_code
}

output "github_oauth_callback_url" {
  description = "Authorization callback URL to register in the GitHub OAuth App."
  value       = "${aws_apigatewayv2_api.github_auth.api_endpoint}/callback"
}

output "github_oauth_secret_arn" {
  description = "Populate this secret with a JSON object containing client_id and client_secret outside Terraform."
  value       = aws_secretsmanager_secret.github_oauth.arn
}
output "radius_instance_id" {
  description = "Private FreeRADIUS instance; manage through SSM, not SSH."
  value       = aws_instance.radius.id
}

output "mfa_table_name" {
  description = "Encrypted OTP records; deleting an active record requires verified recovery."
  value       = aws_dynamodb_table.mfa.name
}