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

  mock_data "aws_iam_role" {
    defaults = {
      name = "AmazonAppStreamServiceAccess"
      path = "/service-role/"
      arn  = "arn:aws:iam::123456789012:role/service-role/AmazonAppStreamServiceAccess"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/test-role"
    }
  }

  mock_resource "aws_apigatewayv2_api" {
    defaults = {
      api_endpoint  = "https://example.execute-api.ap-northeast-1.amazonaws.com"
      execution_arn = "arn:aws:execute-api:ap-northeast-1:123456789012:example"
    }
  }

  override_resource {
    target = aws_appautoscaling_policy.scale_out
    values = {
      arn = "arn:aws:autoscaling:ap-northeast-1:123456789012:scalingPolicy:12345678-1234-1234-1234-123456789012:resource/appstream/fleet/test-fleet:policyName/scale-out"
    }
  }

  override_resource {
    target = aws_appautoscaling_policy.scale_in
    values = {
      arn = "arn:aws:autoscaling:ap-northeast-1:123456789012:scalingPolicy:12345678-1234-1234-1234-123456789012:resource/appstream/fleet/test-fleet:policyName/scale-in"
    }
  }

  mock_resource "aws_lambda_function" {
    defaults = {
      invoke_arn = "arn:aws:apigateway:ap-northeast-1:lambda:path/2015-03-31/functions/arn:aws:lambda:ap-northeast-1:123456789012:function:test/invocations"
    }
  }

  mock_resource "aws_lambda_layer_version" {
    defaults = {
      arn = "arn:aws:lambda:ap-northeast-1:123456789012:layer:test-qr:1"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:ap-northeast-1:123456789012:key/12345678-1234-1234-1234-123456789012"
      key_id = "12345678-1234-1234-1234-123456789012"
    }
  }

  mock_resource "aws_directory_service_directory" {
    defaults = {
      id                = "d-1234567890"
      dns_ip_addresses  = ["10.20.0.10", "10.20.16.10"]
      security_group_id = "sg-1234567890abcdef0"
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-1234567890abcdef0"
    }
  }

  mock_resource "aws_instance" {
    defaults = {
      id         = "i-1234567890abcdef0"
      private_ip = "10.20.0.20"
    }
  }
}

mock_provider "random" {}

variables {
  # Keep the local-exec provisioner from calling the real AppStream API.
  disable_home_folders_with_cli = false
  wait_for_radius_ready         = false
  github_organization           = "example-org"
  appstream_image_name          = "published-test-image"
}

run "linux_single_session_scaling" {
  command = plan

  assert {
    condition = (
      aws_appstream_stack.main.application_settings[0].enabled == false &&
      length(aws_appstream_stack.main.storage_connectors) == 0 &&
      aws_appstream_fleet_stack_association.main.fleet_name == aws_appstream_fleet.main.name &&
      aws_appstream_fleet_stack_association.main.stack_name == aws_appstream_stack.main.name
    )
    error_message = "The Linux stack must disable application settings persistence and home folders, and associate with the fleet."
  }

  assert {
    condition = (
      aws_appstream_fleet.main.fleet_type == "ALWAYS_ON" &&
      aws_appstream_fleet.main.instance_type == "stream.standard.xlarge" &&
      aws_appstream_fleet.main.image_name == "published-test-image" &&
      aws_appstream_fleet.main.max_sessions_per_instance == null &&
      aws_appstream_fleet.main.compute_capacity[0].desired_instances == 1
    )
    error_message = "The Linux fleet must use single-session Always-On instances with initial capacity one."
  }

  assert {
    condition = (
      aws_appautoscaling_target.fleet.min_capacity == 1 &&
      aws_appautoscaling_target.fleet.max_capacity == 22 &&
      aws_appautoscaling_target.fleet.resource_id == "fleet/${aws_appstream_fleet.main.name}" &&
      aws_appautoscaling_target.fleet.scalable_dimension == "appstream:fleet:DesiredCapacity" &&
      aws_appautoscaling_target.fleet.service_namespace == "appstream"
    )
    error_message = "Instance scaling must retain at least one instance and allow 20 users plus spare capacity."
  }

  assert {
    condition = (
      aws_appautoscaling_scheduled_action.business_hours_start.schedule == "cron(30 8 * * ? *)" &&
      aws_appautoscaling_scheduled_action.business_hours_start.timezone == "Asia/Tokyo" &&
      aws_appautoscaling_scheduled_action.business_hours_start.scalable_target_action[0].min_capacity == "1" &&
      aws_appautoscaling_scheduled_action.business_hours_start.scalable_target_action[0].max_capacity == "22" &&
      aws_appautoscaling_scheduled_action.business_hours_start.resource_id == aws_appautoscaling_target.fleet.resource_id &&
      aws_appautoscaling_scheduled_action.business_hours_end.schedule == "cron(0 17 * * ? *)" &&
      aws_appautoscaling_scheduled_action.business_hours_end.timezone == "Asia/Tokyo" &&
      aws_appautoscaling_scheduled_action.business_hours_end.scalable_target_action[0].min_capacity == "0" &&
      aws_appautoscaling_scheduled_action.business_hours_end.scalable_target_action[0].max_capacity == "22" &&
      aws_appautoscaling_scheduled_action.business_hours_end.resource_id == aws_appautoscaling_target.fleet.resource_id
    )
    error_message = "Always-On capacity must be provisioned from 08:30 so it is ready by 09:00, kept until 17:00 JST every day, and allowed to reach zero otherwise."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.available_capacity.comparison_operator == "GreaterThanOrEqualToThreshold" &&
      aws_cloudwatch_metric_alarm.available_capacity.threshold == 1 &&
      aws_cloudwatch_metric_alarm.available_capacity.evaluation_periods == 1 &&
      aws_cloudwatch_metric_alarm.available_capacity.treat_missing_data == "notBreaching" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.expression if q.return_data == true]) == "IF((in_use >= 1 AND available <= 1) OR (FILL(insufficient, 0) >= 1 AND FILL(pending, 0) == 0), 1, 0)" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.metric[0].metric_name if q.id == "insufficient"]) == "InsufficientCapacityError" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.metric[0].stat if q.id == "insufficient"]) == "Sum" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.metric[0].metric_name if q.id == "pending"]) == "PendingCapacity" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.metric[0].stat if q.id == "available"]) == "Minimum" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.metric[0].metric_name if q.id == "available"]) == "AvailableCapacity" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.metric[0].metric_name if q.id == "in_use"]) == "InUseCapacity" &&
      one([for q in aws_cloudwatch_metric_alarm.available_capacity.metric_query : q.metric[0].dimensions.Fleet if q.id == "available"]) == aws_appstream_fleet.main.name
    )
    error_message = "Scale-out must trigger while sessions are in use and one or fewer spare instances remain, or on a capacity rejection with nothing pending."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.idle_capacity.comparison_operator == "GreaterThanOrEqualToThreshold" &&
      aws_cloudwatch_metric_alarm.idle_capacity.threshold == 1 &&
      aws_cloudwatch_metric_alarm.idle_capacity.evaluation_periods == 5 &&
      aws_cloudwatch_metric_alarm.idle_capacity.treat_missing_data == "notBreaching" &&
      one([for q in aws_cloudwatch_metric_alarm.idle_capacity.metric_query : q.expression if q.return_data == true]) == "IF(available >= 3 OR (in_use == 0 AND available >= 2), 1, 0)" &&
      one([for q in aws_cloudwatch_metric_alarm.idle_capacity.metric_query : q.metric[0].period if q.id == "available"]) == 60 &&
      one([for q in aws_cloudwatch_metric_alarm.idle_capacity.metric_query : q.metric[0].dimensions.Fleet if q.id == "in_use"]) == aws_appstream_fleet.main.name
    )
    error_message = "Scale-in must wait five minutes and never remove the last spare instance."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.unused_capacity.evaluation_periods == 30 &&
      aws_cloudwatch_metric_alarm.unused_capacity.threshold == 1 &&
      aws_cloudwatch_metric_alarm.unused_capacity.treat_missing_data == "notBreaching" &&
      one([for q in aws_cloudwatch_metric_alarm.unused_capacity.metric_query : q.expression if q.return_data == true]) == "IF(in_use == 0 AND available >= 1, 1, 0)"
    )
    error_message = "The last idle instance must be released after 30 minutes without sessions."
  }
}

run "github_launch_service" {
  command = apply

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.available_capacity.alarm_actions == toset([aws_appautoscaling_policy.scale_out.arn]) &&
      aws_cloudwatch_metric_alarm.idle_capacity.alarm_actions == toset([aws_appautoscaling_policy.scale_in.arn]) &&
      aws_cloudwatch_metric_alarm.unused_capacity.alarm_actions == toset([aws_appautoscaling_policy.scale_in.arn])
    )
    error_message = "Capacity alarms must invoke their respective scaling policies."
  }

  assert {
    condition = (
      aws_appautoscaling_policy.scale_out.resource_id == aws_appautoscaling_target.fleet.resource_id &&
      aws_appautoscaling_policy.scale_out.step_scaling_policy_configuration[0].adjustment_type == "ChangeInCapacity" &&
      aws_appautoscaling_policy.scale_out.step_scaling_policy_configuration[0].cooldown == 120 &&
      one(aws_appautoscaling_policy.scale_out.step_scaling_policy_configuration[0].step_adjustment).metric_interval_lower_bound == "0" &&
      one(aws_appautoscaling_policy.scale_out.step_scaling_policy_configuration[0].step_adjustment).scaling_adjustment == 1 &&
      aws_appautoscaling_policy.scale_in.resource_id == aws_appautoscaling_target.fleet.resource_id &&
      aws_appautoscaling_policy.scale_in.step_scaling_policy_configuration[0].adjustment_type == "ChangeInCapacity" &&
      aws_appautoscaling_policy.scale_in.step_scaling_policy_configuration[0].cooldown == 300 &&
      one(aws_appautoscaling_policy.scale_in.step_scaling_policy_configuration[0].step_adjustment).metric_interval_lower_bound == "0" &&
      one(aws_appautoscaling_policy.scale_in.step_scaling_policy_configuration[0].step_adjustment).metric_interval_upper_bound == null &&
      one(aws_appautoscaling_policy.scale_in.step_scaling_policy_configuration[0].step_adjustment).scaling_adjustment == -1
    )
    error_message = "Policies must add/remove one instance with matching step bounds and a five-minute scale-in cooldown."
  }

  assert {
    condition     = data.aws_iam_role.appstream.name == "AmazonAppStreamServiceAccess"
    error_message = "AppStream must use the existing, externally managed fixed-name service role."
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
    condition     = toset(keys(aws_apigatewayv2_route.github_auth)) == toset(["GET /", "GET /login", "GET /callback", "GET /session-ended", "GET /desktop", "POST /linux", "GET /linux", "POST /windows", "GET /windows", "GET /favicon.ico", "GET /ops-frontier-logo.svg"])
    error_message = "Only the expected authentication, desktop, and branding asset routes should be exposed."
  }

  assert {
    condition = (
      aws_directory_service_directory.windows.name == "example-org.bustion.ops-frontier.dev" &&
      aws_directory_service_directory.windows.type == "MicrosoftAD" &&
      aws_directory_service_directory.windows.edition == "Standard" &&
      aws_directory_service_directory.windows.enable_directory_data_access &&
      aws_directory_service_directory.windows.vpc_settings[0].vpc_id == aws_vpc.main.id &&
      toset(aws_directory_service_directory.windows.vpc_settings[0].subnet_ids) == toset([for subnet in aws_subnet.private : subnet.id])
    )
    error_message = "Windows must use Standard Managed Microsoft AD with data access in the shared VPC/subnets."
  }

  assert {
    condition = (
      aws_workspaces_directory.windows.directory_id == aws_directory_service_directory.windows.id &&
      aws_workspaces_directory.windows.workspace_creation_properties[0].enable_internet_access == false &&
      aws_workspaces_directory.windows.workspace_creation_properties[0].user_enabled_as_local_administrator == false &&
      aws_workspaces_directory.windows.self_service_permissions[0].switch_running_mode == false
    )
    error_message = "WorkSpaces must register the shared AD and retain private networking and AutoStop billing."
  }

  assert {
    condition = (
      aws_lambda_function.github_auth.environment[0].variables.DIRECTORY_ID == aws_workspaces_directory.windows.id &&
      aws_lambda_function.github_auth.environment[0].variables.USERS_TABLE == aws_dynamodb_table.windows_users.name &&
      aws_lambda_function.github_auth.environment[0].variables.WORKSPACE_BUNDLE_ID == "wsb-96cqc2ksn" &&
      aws_dynamodb_table.windows_users.point_in_time_recovery[0].enabled &&
      length(aws_dynamodb_table.windows_users.ttl) == 0 &&
      random_password.directory_admin.length == 32
    )
    error_message = "Provisioning must use the specified bundle and durable identity bindings, with a generated administrator password."
  }

  assert {
    condition = (
      !contains(flatten([for statement in jsondecode(aws_iam_role_policy.github_auth.policy).Statement : statement.Action]), "workspaces:TerminateWorkspaces") &&
      !contains(flatten([for statement in jsondecode(aws_iam_role_policy.github_auth.policy).Statement : statement.Action]), "ds-data:DeleteUser")
    )
    error_message = "The shared Lambda must not be permitted to delete user data."
  }

  assert {
    condition = (
      contains(
        one([for statement in jsondecode(aws_iam_role_policy.github_auth.policy).Statement : statement.Resource if contains(statement.Action, "workspaces:CreateWorkspaces")]),
        "arn:aws:workspaces:${var.region}:*:workspacebundle/wsb-96cqc2ksn"
      ) &&
      one([for statement in jsondecode(aws_iam_role_policy.github_auth.policy).Statement : statement.Resource if contains(statement.Action, "workspaces:CreateTags")]) == "*"
    )
    error_message = "Workspace creation must authorize the public bundle; CreateTags does not support resource-level permissions."
  }

  assert {
    condition     = aws_appstream_stack.main.redirect_url == "${aws_apigatewayv2_api.github_auth.api_endpoint}/session-ended"
    error_message = "End Session must redirect to the session-ended page on the deployed API."
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
      aws_instance.radius.instance_type == "t4g.micro" &&
      !aws_instance.radius.associate_public_ip_address &&
      aws_instance.radius.subnet_id == aws_subnet.private["0"].id &&
      aws_instance.radius.metadata_options[0].http_tokens == "required" &&
      aws_instance.radius.root_block_device[0].encrypted &&
      length(aws_instance.radius.user_data_base64) <= 21844 &&
      aws_instance.radius.user_data_replace_on_change
    )
    error_message = "RADIUS must run on one small private EC2 host with encrypted disk and IMDSv2."
  }
  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.radius.ip_protocol == "udp" &&
      aws_vpc_security_group_ingress_rule.radius.from_port == 1812 &&
      aws_vpc_security_group_ingress_rule.radius.to_port == 1812 &&
      aws_vpc_security_group_ingress_rule.radius.referenced_security_group_id == aws_directory_service_directory.windows.security_group_id &&
      aws_vpc_security_group_ingress_rule.radius.cidr_ipv4 == null
    )
    error_message = "Only Directory Service may reach the RADIUS port."
  }
  assert {
    condition = (
      strcontains(file("${path.module}/../scripts/wait_radius_ready.sh"), "cloud-init status --wait") &&
      strcontains(file("${path.module}/../scripts/wait_radius_ready.sh"), "systemctl is-active --quiet freeradius") &&
      strcontains(file("${path.module}/../scripts/wait_radius_ready.sh"), "freeradius -C") &&
      strcontains(file("${path.module}/radius/cloud-init.yaml.tftpl"), "rm -f /etc/freeradius/3.0/mods-enabled/eap")
    )
    error_message = "Directory MFA activation must wait for successful cloud-init and a running, valid RADIUS service."
  }

  assert {
    condition = (
      aws_directory_service_radius_settings.windows.authentication_protocol == "PAP" &&
      aws_directory_service_radius_settings.windows.directory_id == aws_directory_service_directory.windows.id &&
      aws_directory_service_radius_settings.windows.radius_servers == toset([aws_instance.radius.private_ip]) &&
      aws_directory_service_radius_settings.windows.radius_port == 1812 &&
      aws_directory_service_radius_settings.windows.shared_secret == random_password.radius.result
    )
    error_message = "The directory must require OTP validation against the private RADIUS host."
  }
  assert {
    condition = (
      aws_dynamodb_table.mfa.hash_key == "login" &&
      aws_dynamodb_table.mfa.point_in_time_recovery[0].enabled &&
      length(aws_dynamodb_table.mfa.ttl) == 0 &&
      aws_kms_key.mfa.enable_key_rotation &&
      random_password.radius.length == 64 &&
      !random_password.radius.special &&
      aws_lambda_function.github_auth.environment[0].variables.MFA_TABLE == aws_dynamodb_table.mfa.name &&
      aws_lambda_function.github_auth.environment[0].variables.MFA_KEY_ARN == aws_kms_key.mfa.arn &&
      aws_lambda_function.github_auth.layers == tolist([aws_lambda_layer_version.qr.arn])
    )
    error_message = "OTP records must be durable and encrypted, and Lambda must load the QR layer."
  }
  assert {
    condition = (
      !contains(flatten([for s in jsondecode(aws_iam_role_policy.radius.policy).Statement : s.Action]), "kms:Encrypt") &&
      !contains(flatten([for s in jsondecode(aws_iam_role_policy.radius.policy).Statement : s.Action]), "dynamodb:PutItem") &&
      !contains(flatten([for s in jsondecode(aws_iam_role_policy.radius.policy).Statement : s.Action]), "ds:ResetUserPassword")
    )
    error_message = "RADIUS may verify OTPs, not register seeds or reset AD passwords."
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
      toset(jsondecode(aws_iam_role_policy.github_auth.policy).Statement[4].Action) ==
      toset(["appstream:DescribeFleets"]) &&
      jsondecode(aws_iam_role_policy.github_auth.policy).Statement[4].Resource == "*" &&
      toset(jsondecode(aws_iam_role_policy.github_auth.policy).Statement[5].Action) ==
      toset(["appstream:UpdateFleet"]) &&
      jsondecode(aws_iam_role_policy.github_auth.policy).Statement[5].Resource == aws_appstream_fleet.main.arn
    )
    error_message = "Lambda must be able to inspect capacity and wake only the configured fleet; DescribeFleets requires a wildcard resource."
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

run "home_folder_removal_enabled_by_default" {
  command = plan

  variables {
    disable_home_folders_with_cli = true
  }

  assert {
    condition     = length(terraform_data.disable_home_folders) == 1
    error_message = "Existing stacks must have their home folder connector removed by default."
  }
}

run "radius_readiness_enabled_by_default" {
  command = plan

  variables {
    wait_for_radius_ready = true
  }

  assert {
    condition     = length(terraform_data.radius_ready) == 1
    error_message = "Production applies must wait for actual RADIUS bootstrap and service readiness."
  }
}

run "appstream_service_role_wrong_path_rejected" {
  command = plan

  override_data {
    target = data.aws_iam_role.appstream
    values = {
      name = "AmazonAppStreamServiceAccess"
      path = "/"
    }
  }

  expect_failures = [data.aws_iam_role.appstream]
}
