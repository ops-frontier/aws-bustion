resource "aws_secretsmanager_secret" "github_oauth" {
  name_prefix = "github-oauth-${local.name_suffix}-"
  description = "GitHub OAuth client_id and client_secret; populate outside Terraform."
}

resource "aws_dynamodb_table" "oauth_state" {
  name         = "oauth-state-${local.name_suffix}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "state_id"

  attribute {
    name = "state_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_apigatewayv2_api" "github_auth" {
  name          = "github-auth-${local.name_suffix}"
  protocol_type = "HTTP"
}

resource "aws_cloudwatch_log_group" "github_auth" {
  name              = "/aws/lambda/github-auth-${local.name_suffix}"
  retention_in_days = 30
}

resource "aws_iam_role" "github_auth" {
  name = "github-auth-${local.name_suffix}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "github_auth" {
  name = "github-auth-${local.name_suffix}"
  role = aws_iam_role.github_auth.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.github_auth.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.github_oauth.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.oauth_state.arn
      },
      {
        Effect   = "Allow"
        Action   = ["appstream:CreateStreamingURL"]
        Resource = [aws_appstream_stack.main.arn, aws_appstream_fleet.main.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["appstream:DescribeFleets"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["appstream:UpdateFleet"]
        Resource = aws_appstream_fleet.main.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.windows_users.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ds:AccessDSData", "ds:ResetUserPassword", "ds-data:CreateUser", "ds-data:DescribeUser"]
        Resource = "arn:aws:ds:${var.region}:${data.aws_caller_identity.current.account_id}:directory/${aws_directory_service_directory.windows.id}"
      },
      {
        Effect   = "Allow"
        Action   = ["workspaces:DescribeWorkspaces", "workspaces:DescribeWorkspaceDirectories"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["workspaces:CreateWorkspaces"]
        Resource = [
          "arn:aws:workspaces:${var.region}:${data.aws_caller_identity.current.account_id}:directory/${aws_workspaces_directory.windows.id}",
          "arn:aws:workspaces:${var.region}:*:workspacebundle/wsb-96cqc2ksn",
          "arn:aws:workspaces:${var.region}:${data.aws_caller_identity.current.account_id}:workspace/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["workspaces:CreateTags"]
        Resource = "*"
        Condition = {
          "ForAllValues:StringEquals" = {
            "aws:TagKeys" = ["GitHubUserId"]
          }
        }
      }
    ]
  })
}

data "archive_file" "github_auth" {
  type        = "zip"
  source_file = "${path.module}/lambda/github_auth.py"
  output_path = "${path.module}/.terraform/github-auth.zip"
}

resource "aws_lambda_function" "github_auth" {
  function_name    = "github-auth-${local.name_suffix}"
  role             = aws_iam_role.github_auth.arn
  runtime          = "python3.13"
  handler          = "github_auth.lambda_handler"
  filename         = data.archive_file.github_auth.output_path
  source_code_hash = data.archive_file.github_auth.output_base64sha256
  timeout          = 29
  memory_size      = 128

  environment {
    variables = {
      BASE_URL            = aws_apigatewayv2_api.github_auth.api_endpoint
      GITHUB_ORGANIZATION = var.github_organization
      OAUTH_SECRET_ARN    = aws_secretsmanager_secret.github_oauth.arn
      STATE_TABLE         = aws_dynamodb_table.oauth_state.name
      APPSTREAM_STACK     = aws_appstream_stack.main.name
      APPSTREAM_FLEET     = aws_appstream_fleet.main.name
      USERS_TABLE         = aws_dynamodb_table.windows_users.name
      DIRECTORY_ID        = aws_workspaces_directory.windows.id
      WORKSPACE_BUNDLE_ID = "wsb-96cqc2ksn"
    }
  }

  depends_on = [
    aws_iam_role_policy.github_auth,
    aws_cloudwatch_log_group.github_auth,
    aws_appstream_fleet_stack_association.main
  ]
}

resource "aws_apigatewayv2_integration" "github_auth" {
  api_id                 = aws_apigatewayv2_api.github_auth.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.github_auth.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_route" "github_auth" {
  for_each = toset([
    "GET /", "GET /login", "GET /callback", "GET /session-ended",
    "GET /desktop", "POST /linux", "GET /linux", "POST /windows", "GET /windows"
  ])

  api_id    = aws_apigatewayv2_api.github_auth.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.github_auth.id}"
}

resource "aws_apigatewayv2_stage" "github_auth" {
  api_id      = aws_apigatewayv2_api.github_auth.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

resource "aws_lambda_permission" "github_auth" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_auth.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.github_auth.execution_arn}/*/*/*"
}
