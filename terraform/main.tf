data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

locals {
  name_suffix = "bustion-${var.github_organization}"
  private_cidrs = {
    for index, cidr in ["10.20.0.0/20", "10.20.16.0/20"] : index => cidr
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "vpc-${local.name_suffix}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "igw-${local.name_suffix}"
  }
}

resource "aws_subnet" "private" {
  for_each = local.private_cidrs

  vpc_id                  = aws_vpc.main.id
  cidr_block              = each.value
  availability_zone       = data.aws_availability_zones.available.names[each.key]
  map_public_ip_on_launch = false

  tags = {
    Name = "subnet-private-${each.key + 1}-${local.name_suffix}"
  }
}

resource "aws_nat_gateway" "regional" {
  availability_mode = "regional"
  vpc_id            = aws_vpc.main.id

  depends_on = [aws_internet_gateway.main]

  tags = {
    Name = "nat-${local.name_suffix}"
  }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.regional.id
  }

  tags = {
    Name = "rtb-private-${local.name_suffix}"
  }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "appstream" {
  name        = "appstream-${local.name_suffix}"
  description = "Egress for AppStream sessions through the regional NAT gateway."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "sg-appstream-${local.name_suffix}"
  }
}

# The account-wide AppStream service role is shared with other AppStream
# deployments and managed outside this configuration. The removed blocks drop
# it from state without deleting it, so destroy never touches it.
removed {
  from = aws_iam_role.appstream

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_iam_role_policy_attachment.appstream

  lifecycle {
    destroy = false
  }
}

data "aws_iam_role" "appstream" {
  name = "AmazonAppStreamServiceAccess"

  lifecycle {
    postcondition {
      condition     = self.path == "/service-role/"
      error_message = "AmazonAppStreamServiceAccess must exist under the /service-role/ IAM path; AppStream rejects a role at another path."
    }
  }
}

resource "aws_appstream_fleet" "main" {
  name                           = "fleet-${local.name_suffix}"
  image_name                     = var.appstream_image_name
  instance_type                  = "stream.standard.xlarge"
  fleet_type                     = "ALWAYS_ON"
  stream_view                    = "APP"
  enable_default_internet_access = false

  compute_capacity {
    desired_instances = var.desired_capacity
  }

  vpc_config {
    subnet_ids         = [for subnet in aws_subnet.private : subnet.id]
    security_group_ids = [aws_security_group.appstream.id]
  }

  depends_on = [data.aws_iam_role.appstream]

  lifecycle {
    ignore_changes = [compute_capacity[0].desired_instances]
  }

  tags = {
    Name = "fleet-${local.name_suffix}"
  }
}

resource "aws_appstream_stack" "main" {
  name         = "stack-${local.name_suffix}"
  description  = "Linux developer bastion for ${var.github_organization}."
  feedback_url = "https://github.com/${var.github_organization}"
  redirect_url = "${aws_apigatewayv2_api.github_auth.api_endpoint}/session-ended"

  application_settings {
    enabled = false
  }

  user_settings {
    action     = "CLIPBOARD_COPY_FROM_LOCAL_DEVICE"
    permission = "ENABLED"
  }

  user_settings {
    action     = "CLIPBOARD_COPY_TO_LOCAL_DEVICE"
    permission = "ENABLED"
  }

  user_settings {
    action     = "FILE_UPLOAD"
    permission = "ENABLED"
  }

  user_settings {
    action     = "FILE_DOWNLOAD"
    permission = "ENABLED"
  }

  depends_on = [data.aws_iam_role.appstream]

  tags = {
    Name = "stack-${local.name_suffix}"
  }
}

# The AWS provider ignores storage_connectors changes on update, so removing the
# block alone would leave home folders enabled on an existing stack.
resource "terraform_data" "disable_home_folders" {
  count            = var.disable_home_folders_with_cli ? 1 : 0
  triggers_replace = [aws_appstream_stack.main.id]

  provisioner "local-exec" {
    command = join(" ", [
      "env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u AWS_SECURITY_TOKEN",
      "aws --profile terraform-role --region ${var.region} --no-cli-pager",
      "appstream update-stack --name ${aws_appstream_stack.main.name}",
      "--attributes-to-delete STORAGE_CONNECTORS --output text --query Stack.Name",
    ])
  }
}

resource "aws_appstream_fleet_stack_association" "main" {
  fleet_name = aws_appstream_fleet.main.name
  stack_name = aws_appstream_stack.main.name

  # The fleet keeps its name when replaced (e.g. fleet_type changes), so the
  # association must be removed before the old fleet can be deleted.
  lifecycle {
    replace_triggered_by = [aws_appstream_fleet.main.arn]
  }
}

resource "aws_appautoscaling_target" "fleet" {
  max_capacity       = var.max_capacity
  min_capacity       = var.min_capacity
  resource_id        = "fleet/${aws_appstream_fleet.main.name}"
  scalable_dimension = "appstream:fleet:DesiredCapacity"
  service_namespace  = "appstream"

  # Scheduled actions own the minimum after creation.
  lifecycle {
    ignore_changes       = [min_capacity]
    replace_triggered_by = [aws_appstream_fleet.main.arn]
  }
}

resource "aws_appautoscaling_scheduled_action" "business_hours_start" {
  name               = "business-hours-start-${local.name_suffix}"
  service_namespace  = aws_appautoscaling_target.fleet.service_namespace
  resource_id        = aws_appautoscaling_target.fleet.resource_id
  scalable_dimension = aws_appautoscaling_target.fleet.scalable_dimension
  schedule           = var.business_hours_start_schedule
  timezone           = var.business_hours_timezone

  scalable_target_action {
    min_capacity = var.min_capacity
    max_capacity = var.max_capacity
  }

  lifecycle {
    replace_triggered_by = [aws_appautoscaling_target.fleet]
  }
}

resource "aws_appautoscaling_scheduled_action" "business_hours_end" {
  name               = "business-hours-end-${local.name_suffix}"
  service_namespace  = aws_appautoscaling_target.fleet.service_namespace
  resource_id        = aws_appautoscaling_target.fleet.resource_id
  scalable_dimension = aws_appautoscaling_target.fleet.scalable_dimension
  schedule           = var.business_hours_end_schedule
  timezone           = var.business_hours_timezone

  scalable_target_action {
    min_capacity = 0
    max_capacity = var.max_capacity
  }

  lifecycle {
    replace_triggered_by = [aws_appautoscaling_target.fleet]
  }
}

resource "aws_appautoscaling_policy" "scale_out" {
  name               = "scale-out-${local.name_suffix}"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.fleet.resource_id
  scalable_dimension = aws_appautoscaling_target.fleet.scalable_dimension
  service_namespace  = aws_appautoscaling_target.fleet.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 120
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 1
    }
  }

  lifecycle {
    replace_triggered_by = [aws_appautoscaling_target.fleet]
  }
}

resource "aws_appautoscaling_policy" "scale_in" {
  name               = "scale-in-${local.name_suffix}"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.fleet.resource_id
  scalable_dimension = aws_appautoscaling_target.fleet.scalable_dimension
  service_namespace  = aws_appautoscaling_target.fleet.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 300
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = -1
    }
  }

  lifecycle {
    replace_triggered_by = [aws_appautoscaling_target.fleet]
  }
}

resource "aws_cloudwatch_metric_alarm" "available_capacity" {
  alarm_name          = "appstream-available-${local.name_suffix}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  alarm_description   = "Add an AppStream instance while sessions are in use and one or fewer spare instances remain, or when a launch fails for lack of capacity."
  alarm_actions       = [aws_appautoscaling_policy.scale_out.arn]
  treat_missing_data  = "notBreaching"

  # Gating on in-use sessions keeps the idle fleet at the single minimum spare
  # instead of repeatedly scaling out and back in. Outside business hours the
  # fleet may be empty, so a rejected launch adds one instance unless one is
  # already being provisioned.
  metric_query {
    id          = "scale_out"
    expression  = "IF((in_use >= 1 AND available <= 1) OR (FILL(insufficient, 0) >= 1 AND FILL(pending, 0) == 0), 1, 0)"
    label       = "Scale out needed"
    return_data = true
  }

  metric_query {
    id = "available"

    metric {
      metric_name = "AvailableCapacity"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Minimum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }

  metric_query {
    id = "in_use"

    metric {
      metric_name = "InUseCapacity"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }

  metric_query {
    id = "insufficient"

    metric {
      metric_name = "InsufficientCapacityError"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Sum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }

  metric_query {
    id = "pending"

    metric {
      metric_name = "PendingCapacity"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "idle_capacity" {
  alarm_name          = "appstream-idle-${local.name_suffix}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 5
  threshold           = 1
  alarm_description   = "Remove one idle AppStream instance after surplus spare capacity persists for five minutes."
  alarm_actions       = [aws_appautoscaling_policy.scale_in.arn]
  treat_missing_data  = "notBreaching"

  # Thresholds are one above the scale-out condition so a scale-in never
  # immediately re-triggers a scale-out.
  metric_query {
    id          = "scale_in"
    expression  = "IF(available >= 3 OR (in_use == 0 AND available >= 2), 1, 0)"
    label       = "Scale in allowed"
    return_data = true
  }

  metric_query {
    id = "available"

    metric {
      metric_name = "AvailableCapacity"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Minimum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }

  metric_query {
    id = "in_use"

    metric {
      metric_name = "InUseCapacity"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "unused_capacity" {
  alarm_name          = "appstream-unused-${local.name_suffix}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 30
  threshold           = 1
  alarm_description   = "Remove the last idle AppStream instance after 30 minutes without sessions; the business-hours minimum keeps one instance during the day."
  alarm_actions       = [aws_appautoscaling_policy.scale_in.arn]
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "unused"
    expression  = "IF(in_use == 0 AND available >= 1, 1, 0)"
    label       = "Fleet unused"
    return_data = true
  }

  metric_query {
    id = "available"

    metric {
      metric_name = "AvailableCapacity"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Minimum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }

  metric_query {
    id = "in_use"

    metric {
      metric_name = "InUseCapacity"
      namespace   = "AWS/AppStream"
      period      = 60
      stat        = "Maximum"
      dimensions = {
        Fleet = aws_appstream_fleet.main.name
      }
    }
  }
}
