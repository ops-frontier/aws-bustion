resource "aws_kms_key" "mfa" {
  description             = "Envelope encryption for WorkSpaces TOTP seeds."
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "mfa" {
  name          = "alias/mfa-${local.name_suffix}"
  target_key_id = aws_kms_key.mfa.key_id
}

resource "aws_dynamodb_table" "mfa" {
  name         = "mfa-${local.name_suffix}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "login"

  attribute {
    name = "login"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "random_password" "radius" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "radius" {
  name_prefix = "radius-${local.name_suffix}-"
  description = "Shared secret for Directory Service and the private RADIUS server."
}

resource "aws_secretsmanager_secret_version" "radius" {
  secret_id     = aws_secretsmanager_secret.radius.id
  secret_string = random_password.radius.result
}

data "aws_ssm_parameter" "radius_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

resource "aws_security_group" "radius" {
  name        = "radius-${local.name_suffix}"
  description = "Private RADIUS endpoint; only Directory Service may authenticate."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "radius" {
  security_group_id            = aws_security_group.radius.id
  referenced_security_group_id = aws_directory_service_directory.windows.security_group_id
  ip_protocol                  = "udp"
  from_port                    = 1812
  to_port                      = 1812
}

resource "aws_vpc_security_group_egress_rule" "directory_radius" {
  security_group_id            = aws_directory_service_directory.windows.security_group_id
  referenced_security_group_id = aws_security_group.radius.id
  ip_protocol                  = "udp"
  from_port                    = 1812
  to_port                      = 1812
}

resource "aws_vpc_security_group_egress_rule" "radius_https" {
  security_group_id = aws_security_group.radius.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "radius_packages" {
  security_group_id = aws_security_group.radius.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_iam_role" "radius" {
  name = "radius-${local.name_suffix}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "radius_ssm" {
  role       = aws_iam_role.radius.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "radius" {
  role = aws_iam_role.radius.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.mfa.arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = aws_kms_key.mfa.arn
        Condition = {
          StringEquals = { "kms:EncryptionContext:purpose" = "bustion-totp" }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.radius.arn
      }
    ]
  })
}

resource "aws_iam_instance_profile" "radius" {
  name = "radius-${local.name_suffix}"
  role = aws_iam_role.radius.name
}

resource "aws_instance" "radius" {
  ami                         = data.aws_ssm_parameter.radius_ami.value
  instance_type               = "t4g.micro"
  subnet_id                   = aws_subnet.private["0"].id
  associate_public_ip_address = false
  vpc_security_group_ids      = [aws_security_group.radius.id]
  iam_instance_profile        = aws_iam_instance_profile.radius.name
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(templatefile("${path.module}/radius/cloud-init.yaml.tftpl", {
    config = base64encode(jsonencode({
      region  = var.region
      table   = aws_dynamodb_table.mfa.name
      key     = aws_kms_key.mfa.arn
      secret  = aws_secretsmanager_secret.radius.arn
      version = aws_secretsmanager_secret_version.radius.version_id
      clients = sort(tolist(aws_directory_service_directory.windows.dns_ip_addresses))
    }))
    mfa         = filebase64("${path.module}/lambda/mfa.py")
    radius_auth = filebase64("${path.module}/radius/radius_auth.py")
    configure   = filebase64("${path.module}/radius/configure.py")
    server      = filebase64("${path.module}/radius/server.conf")
    python3     = filebase64("${path.module}/radius/python3.conf")
  }))

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tags = { Name = "radius-${local.name_suffix}" }

  lifecycle {
    replace_triggered_by = [aws_secretsmanager_secret_version.radius]
  }

  depends_on = [
    aws_iam_role_policy.radius, aws_iam_role_policy_attachment.radius_ssm,
    aws_secretsmanager_secret_version.radius,
    aws_route_table_association.private, aws_vpc_security_group_egress_rule.radius_https,
    aws_vpc_security_group_egress_rule.radius_packages,
  ]
}

resource "terraform_data" "radius_ready" {
  count = var.wait_for_radius_ready ? 1 : 0

  triggers_replace = [aws_instance.radius.id, filesha256("${path.module}/../scripts/wait_radius_ready.sh")]

  provisioner "local-exec" {
    command = "bash \"${path.module}/../scripts/wait_radius_ready.sh\""
    environment = {
      RADIUS_INSTANCE_ID = aws_instance.radius.id
      AWS_REGION         = var.region
    }
  }
}

resource "aws_directory_service_radius_settings" "windows" {
  directory_id            = aws_directory_service_directory.windows.id
  authentication_protocol = "PAP"
  display_label           = "認証アプリの6桁コード"
  radius_port             = 1812
  radius_retries          = 1
  radius_servers          = [aws_instance.radius.private_ip]
  radius_timeout          = 10
  shared_secret           = random_password.radius.result

  lifecycle {
    # Directory Service omits the secret on reads; rotate by replacing both peers.
    ignore_changes       = [shared_secret]
    replace_triggered_by = [random_password.radius]
  }

  depends_on = [
    aws_vpc_security_group_ingress_rule.radius, aws_vpc_security_group_egress_rule.directory_radius,
    terraform_data.radius_ready,
  ]
}

resource "aws_cloudwatch_metric_alarm" "radius_status" {
  alarm_name          = "radius-status-${local.name_suffix}"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "breaching"
  dimensions          = { InstanceId = aws_instance.radius.id }
  alarm_description   = "Single RADIUS host unavailable: WorkSpaces MFA sign-in fails closed."
}
