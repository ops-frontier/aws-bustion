mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-northeast-1a", "ap-northeast-1c"]
    }
  }

  mock_resource "aws_appstream_stack" {
    defaults = {
      arn = "arn:aws:appstream:ap-northeast-1:123456789012:stack/test-stack"
    }
  }

  mock_resource "aws_appstream_fleet" {
    defaults = {
      arn = "arn:aws:appstream:ap-northeast-1:123456789012:fleet/test-fleet"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/test-role"
    }
  }

  mock_resource "aws_appautoscaling_policy" {
    defaults = {
      arn = "arn:aws:autoscaling:ap-northeast-1:123456789012:scalingPolicy:12345678-1234-1234-1234-123456789012:resource/appstream/fleet/test-fleet:policyName/test-policy"
    }
  }

  mock_resource "aws_apigatewayv2_api" {
    defaults = {
      api_endpoint  = "https://example.execute-api.ap-northeast-1.amazonaws.com"
      execution_arn = "arn:aws:execute-api:ap-northeast-1:123456789012:example"
    }
  }

  mock_resource "aws_lambda_function" {
    defaults = {
      invoke_arn = "arn:aws:apigateway:ap-northeast-1:lambda:path/2015-03-31/functions/arn:aws:lambda:ap-northeast-1:123456789012:function:test/invocations"
    }
  }
}

variables {
  github_organization  = "example-org"
  appstream_image_name = "published-test-image"
}

run "github_launch_service" {
  command = apply

  assert {
    condition     = aws_iam_role.appstream.name == "AmazonAppStreamServiceAccess"
    error_message = "AppStream requires the fixed-name service role, not a service-linked role."
  }

  assert {
    condition     = aws_iam_role.appstream.path == "/service-role/"
    error_message = "AppStream looks up AmazonAppStreamServiceAccess under the /service-role/ IAM path."
  }

  assert {
    condition = jsondecode(aws_iam_role.appstream.assume_role_policy) == {
      Version = "2012-10-17"
      Statement = [{
        Effect    = "Allow"
        Principal = { Service = "appstream.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }]
    }
    error_message = "The AppStream service must be able to assume its service role."
  }

  assert {
    condition = (
      aws_iam_role_policy_attachment.appstream.role == aws_iam_role.appstream.name &&
      aws_iam_role_policy_attachment.appstream.policy_arn == "arn:aws:iam::aws:policy/service-role/AmazonAppStreamServiceAccess"
    )
    error_message = "The AppStream service role must have the AWS managed service access policy attached."
  }

  assert {
    condition     = aws_lambda_function.github_auth.environment[0].variables.GITHUB_ORGANIZATION == "example-org"
    error_message = "Lambda must authorize membership in the configured organization."
  }

  assert {
    condition     = aws_lambda_function.github_auth.environment[0].variables.BASE_URL == aws_apigatewayv2_api.github_auth.api_endpoint
    error_message = "Lambda redirects must use the deployed API endpoint."
  }

  assert {
    condition     = toset(keys(aws_apigatewayv2_route.github_auth)) == toset(["GET /", "GET /login", "GET /callback"])
    error_message = "Only the expected login and callback routes should be exposed."
  }

  assert {
    condition     = aws_dynamodb_table.oauth_state.ttl[0].enabled && aws_dynamodb_table.oauth_state.ttl[0].attribute_name == "expires_at"
    error_message = "Expired OAuth state must be cleaned up using expires_at."
  }

  assert {
    condition = (
      aws_lambda_function.github_auth.environment[0].variables.OAUTH_SECRET_ARN == aws_secretsmanager_secret.github_oauth.arn &&
      aws_lambda_function.github_auth.environment[0].variables.STATE_TABLE == aws_dynamodb_table.oauth_state.name &&
      aws_lambda_function.github_auth.environment[0].variables.APPSTREAM_STACK == aws_appstream_stack.main.name &&
      aws_lambda_function.github_auth.environment[0].variables.APPSTREAM_FLEET == aws_appstream_fleet.main.name
    )
    error_message = "Lambda must use the dedicated secret/state table and existing stack/fleet."
  }

  assert {
    condition = (
      toset(jsondecode(aws_iam_role_policy.github_auth.policy).Statement[3].Resource) ==
      toset([aws_appstream_stack.main.arn, aws_appstream_fleet.main.arn])
    )
    error_message = "Streaming URL permissions must be scoped to the configured stack and fleet."
  }

  assert {
    condition = (
      output.appstream_login_url == "${aws_apigatewayv2_api.github_auth.api_endpoint}/login" &&
      output.github_oauth_callback_url == "${aws_apigatewayv2_api.github_auth.api_endpoint}/callback" &&
      output.github_oauth_secret_arn == aws_secretsmanager_secret.github_oauth.arn
    )
    error_message = "Setup outputs must match the resources used by Lambda."
  }
}
