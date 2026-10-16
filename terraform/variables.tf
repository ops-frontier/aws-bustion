variable "region" {
  description = "AWS region for the AppStream environment."
  type        = string
  default     = "ap-northeast-1"
}

variable "github_organization" {
  description = "GitHub organization used to suffix AWS resource names."
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
  description = "Minimum number of AppStream instances."
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Maximum number of AppStream instances."
  type        = number
  default     = 4
}

variable "desired_capacity" {
  description = "Initial number of AppStream instances."
  type        = number
  default     = 1
}