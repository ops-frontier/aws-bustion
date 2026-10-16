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
  name        = "sg-appstream-${local.name_suffix}"
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

resource "aws_iam_service_linked_role" "appstream" {
  aws_service_name = "appstream.amazonaws.com"
}

resource "aws_appstream_fleet" "main" {
  name                         = "fleet-${local.name_suffix}"
  image_name                   = var.appstream_image_name
  instance_type                = "stream.standard.xlarge"
  fleet_type                   = "ON_DEMAND"
  stream_view                  = "APP"
  max_sessions_per_instance    = 7
  enable_default_internet_access = false

  compute_capacity {
    desired_instances = var.desired_capacity
  }

  vpc_config {
    subnet_ids         = [for subnet in aws_subnet.private : subnet.id]
    security_group_ids = [aws_security_group.appstream.id]
  }

  depends_on = [aws_iam_service_linked_role.appstream]

  tags = {
    Name = "fleet-${local.name_suffix}"
  }
}

resource "aws_appstream_stack" "main" {
  name         = "stack-${local.name_suffix}"
  description  = "Linux developer bastion for ${var.github_organization}."
  feedback_url = "https://github.com/${var.github_organization}"
  redirect_url = "https://github.com/${var.github_organization}"

  application_settings {
    enabled        = true
    settings_group = "appstream-${var.github_organization}"
  }

  storage_connectors {
    connector_type = "HOMEFOLDERS"
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

  tags = {
    Name = "stack-${local.name_suffix}"
  }
}

resource "aws_appstream_fleet_stack_association" "main" {
  fleet_name = aws_appstream_fleet.main.name
  stack_name = aws_appstream_stack.main.name
}

resource "aws_appautoscaling_target" "fleet" {
  max_capacity       = var.max_capacity
  min_capacity       = var.min_capacity
  resource_id        = "fleet/${aws_appstream_fleet.main.name}"
  scalable_dimension = "appstream:fleet:DesiredCapacity"
  service_namespace  = "appstream"
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
}

resource "aws_appautoscaling_policy" "scale_in" {
  name               = "scale-in-${local.name_suffix}"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.fleet.resource_id
  scalable_dimension = aws_appautoscaling_target.fleet.scalable_dimension
  service_namespace  = aws_appautoscaling_target.fleet.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 900
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "available_capacity" {
  alarm_name          = "appstream-available-${local.name_suffix}"
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "AvailableCapacity"
  namespace           = "AWS/AppStream"
  period              = 60
  statistic           = "Average"
  threshold           = 3
  alarm_description   = "Add an AppStream instance when three or fewer sessions are available."
  alarm_actions       = [aws_appautoscaling_policy.scale_out.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    Fleet = aws_appstream_fleet.main.name
  }
}

resource "aws_cloudwatch_metric_alarm" "capacity_utilization" {
  alarm_name          = "appstream-utilization-${local.name_suffix}"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 15
  metric_name         = "CapacityUtilization"
  namespace           = "AWS/AppStream"
  period              = 60
  statistic           = "Average"
  threshold           = 30
  alarm_description   = "Remove an AppStream instance after utilization stays below 30% for 15 minutes."
  alarm_actions       = [aws_appautoscaling_policy.scale_in.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    Fleet = aws_appstream_fleet.main.name
  }
}