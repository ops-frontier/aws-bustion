resource "random_password" "directory_admin" {
  length           = 32
  min_upper        = 1
  min_lower        = 1
  min_numeric      = 1
  min_special      = 1
  override_special = "!#$%&*+-=?"
}

resource "aws_directory_service_directory" "windows" {
  name                         = "${var.github_organization}.bustion.ops-frontier.dev"
  short_name                   = upper(substr(var.github_organization, 0, 15))
  description                  = var.github_organization
  type                         = "MicrosoftAD"
  edition                      = "Standard"
  password                     = random_password.directory_admin.result
  enable_directory_data_access = true

  vpc_settings {
    vpc_id     = aws_vpc.main.id
    subnet_ids = [for subnet in aws_subnet.private : subnet.id]
  }

  tags = { Name = "directory-${local.name_suffix}" }
}

# Like AmazonAppStreamServiceAccess, this account-wide role is managed outside
# this deployment so destroying a bastion cannot disrupt other directories.
data "aws_iam_role" "workspaces" {
  name = "workspaces_DefaultRole"
}

resource "aws_workspaces_directory" "windows" {
  directory_id = aws_directory_service_directory.windows.id
  subnet_ids   = [for subnet in aws_subnet.private : subnet.id]

  workspace_creation_properties {
    custom_security_group_id            = aws_security_group.appstream.id
    enable_internet_access              = false
    user_enabled_as_local_administrator = false
  }

  self_service_permissions {
    change_compute_type  = false
    increase_volume_size = false
    rebuild_workspace    = false
    restart_workspace    = true
    switch_running_mode  = false
  }

  tags = { Name = "workspaces-${local.name_suffix}" }

  depends_on = [data.aws_iam_role.workspaces]
}

resource "aws_dynamodb_table" "windows_users" {
  name         = "windows-users-${local.name_suffix}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "principal"

  attribute {
    name = "principal"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}
