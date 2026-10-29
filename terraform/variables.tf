variable "region" {
  description = "AWS region for the AppStream environment."
  type        = string
  default     = "ap-northeast-1"
}

variable "github_organization" {
  description = "GitHub organization whose active members may launch AppStream; also used in resource names."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9-]{1,39}$", var.github_organization))
    error_message = "github_organization must be a GitHub organization name."
  }
}

variable "appstream_image_name" {
  description = "Name of an existing, published AppStream Linux image with Chrome and VS Code."
  type        = string
}

variable "min_capacity" {
  description = "Minimum number of running Always-On AppStream instances during business hours; outside business hours the minimum is zero."
  type        = number
  default     = 1

  validation {
    condition     = var.min_capacity >= 1
    error_message = "min_capacity must be at least 1 so sessions start immediately during business hours."
  }
}

variable "max_capacity" {
  description = "Maximum number of single-session AppStream instances, including spare capacity."
  type        = number
  default     = 22
}

variable "desired_capacity" {
  description = "Initial number of AppStream instances; Application Auto Scaling manages subsequent capacity changes and the launch Lambda wakes an empty fleet."
  type        = number
  default     = 1
}

variable "business_hours_timezone" {
  description = "IANA time zone for the business-hours capacity schedule."
  type        = string
  default     = "Asia/Tokyo"
}

variable "business_hours_start_schedule" {
  description = "Application Auto Scaling schedule that raises the minimum to min_capacity. Defaults to 08:30 every day so the first Always-On instance is provisioned before 09:00."
  type        = string
  default     = "cron(30 8 * * ? *)"

  validation {
    condition     = can(regex("^cron\\(.+\\)$", var.business_hours_start_schedule))
    error_message = "business_hours_start_schedule must be a cron(...) expression."
  }
}

variable "business_hours_end_schedule" {
  description = "Application Auto Scaling schedule that lowers the minimum to zero (default: 17:00 every day)."
  type        = string
  default     = "cron(0 17 * * ? *)"

  validation {
    condition     = can(regex("^cron\\(.+\\)$", var.business_hours_end_schedule))
    error_message = "business_hours_end_schedule must be a cron(...) expression."
  }
}

variable "disable_home_folders_with_cli" {
  description = "Run the AWS CLI (profile terraform-role) to remove storage connectors from the stack, which the AWS provider cannot do in place."
  type        = bool
  default     = true
}

variable "wait_for_radius_ready" {
  description = "Wait for successful RADIUS bootstrap using AWS CLI before enabling MFA. Disable only for offline mock tests."
  type        = bool
  default     = true
}
