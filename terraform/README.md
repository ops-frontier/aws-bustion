# Terraform

This configuration creates the private VPC, regional NAT gateway, AppStream
fleet and stack, and capacity scaling alarms. Resource names include
`bustion-${GITHUB_ORGANIZATION}`.

## Prerequisites

- Terraform 1.6 or later and AWS provider 6.x.
- Codespaces secrets from the root README, including `GITHUB_ORGANIZATION`.
- An already published AppStream image containing the required Linux desktop
  applications. Supply its name as `appstream_image_name`.
- AWS permissions to create VPC, AppStream, IAM service-linked roles,
  Application Auto Scaling, and CloudWatch resources.

The workspace creates a role profile using the supplied source access key.
Terraform and AWS CLI operations using that profile prompt for the MFA code.

```sh
cd terraform
terraform init
terraform plan -var='appstream_image_name=YOUR_PUBLISHED_IMAGE'
terraform apply -var='appstream_image_name=YOUR_PUBLISHED_IMAGE'
```

## Image and sign-in integration

The AppStream fleet consumes a published AppStream image, not an EC2 AMI.
Packer's `amazon-ebs` builder creates an EC2 AMI and cannot publish it as an
AppStream image. Build and publish the AppStream image with an AppStream Image
Builder workflow before applying this configuration.

The Cognito-to-SAML flow in the root README is not directly supported as
written: a Cognito user pool is a SAML service provider, not a SAML assertion
issuer, and GitHub OAuth is not a generic OIDC provider. This configuration
therefore does not create a misleading Cognito/SAML login path. A separate
SAML identity broker or a supported identity provider is required before
end-user authentication can be enabled.

The `HOMEFOLDERS` storage connector persists user home directories. AppStream
application settings persistence is enabled for the stack; users' application
settings are managed by AppStream storage.