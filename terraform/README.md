# Terraform

This configuration creates the private VPC, regional NAT gateway, AppStream
fleet and stack, capacity scaling alarms, and a GitHub OAuth launch service.
Resource names include
`bustion-${BUSTION_GITHUB_ORGANIZATION}`.

## Prerequisites

- Terraform 1.6 or later, AWS provider 6.x, and Archive provider 2.x (>= 2.7).
- Python 3.10+ and AWS CLI v2 (installed by the Dev Container).
- Codespaces secrets from the root README, including `BUSTION_GITHUB_ORGANIZATION`
  and `APPSTREAM_IMAGE_NAME`.
- An already published AppStream image containing the required Linux desktop
  applications. Set its name in the uppercase `APPSTREAM_IMAGE_NAME` secret.
- AWS permissions to create VPC, AppStream, IAM service roles and policy attachments,
  Application Auto Scaling, CloudWatch, API Gateway v2, Lambda, DynamoDB,
  Secrets Manager, and the Lambda execution role/policy, including `iam:PassRole`.
  Application Auto Scaling may also require `iam:CreateServiceLinkedRole` for
  `appstream.application-autoscaling.amazonaws.com` on first use.
  Credential registration also requires `secretsmanager:PutSecretValue`.

The workspace creates a `base` credentials profile and a `terraform-mfa`
configuration profile using the supplied source access key, role ARN, and MFA
serial. Run `python3 scripts/login.py` from the repository root first and enter
the current six-digit OTP at the terminal prompt. The script uses AWS CLI STS
AssumeRole and saves all three temporary credential fields to `terraform-role`
with mode `600`, preserving other credentials profiles. It prints the expiration,
not credentials or the OTP. Errors leave existing credentials unchanged.
Terraform and AWS CLI use `terraform-role` without further MFA prompts; the AWS
provider cannot interactively prompt for MFA itself.

The post-create script maps `BUSTION_GITHUB_ORGANIZATION` and `APPSTREAM_IMAGE_NAME`
to `TF_VAR_github_organization` and `TF_VAR_appstream_image_name`. It saves
these exports in `~/.config/aws-bustion/terraform-env.sh` and loads them through
`~/.bashrc` for new Bash terminals. No lowercase Codespaces secrets or
`-var` arguments are needed.

```sh
python3 scripts/login.py
aws sts get-caller-identity --profile terraform-role
cd terraform
terraform init
terraform plan
terraform apply
terraform output -raw appstream_login_url
terraform output -raw github_oauth_callback_url
terraform output -raw github_oauth_secret_arn
```

After changing a secret, restart the Codespace to refresh its environment,
rerun `bash .devcontainer/post-create.sh` from the repository root, and open
a new Bash terminal (or run `source "$HOME/.config/aws-bustion/terraform-env.sh"`
in the existing one).
Run `python3 scripts/login.py` again afterwards: post-create resets the
credentials file. Also log in again when the temporary session expires
(one hour by default). Rebuild existing Dev Containers to install the Python
feature.

## AppStream service role

AppStream requires the ordinary IAM service role `AmazonAppStreamServiceAccess`,
under the `/service-role/` IAM path, trusted by `appstream.amazonaws.com`,
with the AWS managed policy
`arn:aws:iam::aws:policy/service-role/AmazonAppStreamServiceAccess` attached.
Terraform creates this role and waits for the policy attachment before creating
the fleet and stack. The attachment is recreated when the role changes so that
replacing a role with the same name also restores its managed policy.
It is not a service-linked role: calling
`CreateServiceLinkedRole` for `appstream.amazonaws.com` fails with
`Cannot find Service Linked Role template`. Adding IAM permissions does not fix
that error. The fleet's optional `iam_role_arn` is for streaming instance
credentials and must not be used for this service role.

Before applying, check whether the account already has the role:

```sh
aws iam get-role --role-name AmazonAppStreamServiceAccess --profile terraform-role
```

Only `NoSuchEntity` means it is absent; resolve access or authentication errors
before proceeding. If absent, Terraform creates it on the next apply. If present
(for example, created by the AppStream console), import it from the `terraform`
directory before planning:

```sh
terraform import aws_iam_role.appstream AmazonAppStreamServiceAccess
terraform import aws_iam_role_policy_attachment.appstream \
  AmazonAppStreamServiceAccess/arn:aws:iam::aws:policy/service-role/AmazonAppStreamServiceAccess
```

Import the attachment only if the managed policy is already attached; otherwise
Terraform creates the attachment. Check with
`aws iam list-attached-role-policies --role-name AmazonAppStreamServiceAccess --profile terraform-role`.
This fixed-name role is shared account-wide. Do not import resources owned by
another Terraform state; coordinate ownership with the account administrator.
After import, review the plan for existing role changes, including its IAM path.
Destroying this
configuration also removes the managed role and attachment and can affect other
AppStream deployments in the account.

After the failed service-linked-role creation, run `terraform plan` and
`terraform apply` again with this corrected configuration. Successfully created
resources remain in state; no destroy or state removal is needed for the failed
creation.

### Recovering from `InvalidRoleException`

If `CreateFleet` reports that
`arn:aws:iam::<account-id>:role/service-role/AmazonAppStreamServiceAccess`
is invalid, compare it with the actual role ARN and path:

```sh
aws iam get-role --role-name AmazonAppStreamServiceAccess \
  --query 'Role.{Arn:Arn,Path:Path,Trust:AssumeRolePolicyDocument}' \
  --profile terraform-role
aws iam list-attached-role-policies \
  --role-name AmazonAppStreamServiceAccess --profile terraform-role
```

A role at `arn:aws:iam::<account-id>:role/AmazonAppStreamServiceAccess`
(path `/`) does not match the `/service-role/` ARN in this error.
The configuration explicitly sets `path = "/service-role/"`.
IAM paths cannot be updated in place, so correcting an existing root-path role
requires replacing it. The plan must replace both `aws_iam_role.appstream` and
`aws_iam_role_policy_attachment.appstream`, then create the failed fleet.
The fixed role name prevents creating the replacement before deleting the old
role; coordinate a maintenance window if other AppStream deployments use it.
Do not proceed if the plan would unexpectedly replace other resources.

From the `terraform` directory, review and apply the correction:

```sh
terraform plan
terraform apply
```

Do not destroy the deployment or remove resources from state to recover.
If the path, trust policy, and attachment are correct but the error persists
immediately after role creation, allow a few minutes for IAM propagation and
rerun `terraform plan` and `terraform apply`. The corrected role and attachment
should no longer require replacement; stop and investigate if they do.
Do not force another role replacement, as that restarts IAM propagation.
`depends_on` waits for the IAM API operations to complete, not for AppStream
to see those changes.

AWS provider 6.69.1 has an additional retry limitation: its Fleet creation
handler retries `InvalidRoleException` only when the message contains
`encountered an error because your IAM role`. The message
`encountered an error because your service role` does not match and fails
without that propagation retry. See the
[provider implementation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.69.1/internal/service/appstream/fleet.go).
Increasing SDK retry counts does not change this message filter.
Waiting and rerunning the normal apply avoids recreating the already-correct
role; it does not guarantee resolution if an authorization problem remains.

If the error continues after propagation time, have the account administrator
check the role's effective
permissions, including any permissions boundary or organization policies.
Do not set the fleet's `iam_role_arn` to work around this service-role error.

See the [AWS service role documentation](https://docs.aws.amazon.com/appstream2/latest/developerguide/controlling-access-checking-for-iam-service-access.html).

## Image and sign-in integration

The AppStream fleet consumes a published AppStream image, not an EC2 AMI.
Packer's `amazon-ebs` builder creates an EC2 AMI and cannot publish it as an
AppStream image. Build and publish the AppStream image with an AppStream Image
Builder workflow before applying this configuration.

The public HTTP API exposes `GET /`, `GET /login`, and `GET /callback`.
The Python 3.13 Lambda runs outside the VPC and uses GitHub OAuth with PKCE
and browser-bound, single-use state (10-minute expiry). DynamoDB conditional
deletion enforces expiry and prevents replay independently of TTL cleanup.
Only active members of `github_organization` may launch AppStream.

After applying, follow [GITHUB-SETUP.md](../GITHUB-SETUP.md) to register an OAuth
App with the callback output and populate the secret output with
`{"client_id":"...","client_secret":"..."}`. Terraform manages only the secret
container, never its value. Sign-in returns 503 until credentials are configured.
No Cognito, OIDC wrapper, SAML federation, or AppStream user-pool assignments
are required.

Lambda verifies the authenticated user's organization membership on every
launch, calls `CreateStreamingURL` with a 60-second validity and stable
`gh-<numeric GitHub ID>` user ID, and redirects the browser to that URL.
The existing `APP` stream view is unchanged; no `ApplicationId` is specified.
GitHub tokens are not persisted, and removing organization membership does
not terminate existing AppStream sessions.

The execution role can only read the dedicated secret, put/delete state in
the dedicated table, write its Lambda logs, and create streaming URLs for
the configured stack/fleet. Lambda logs are retained for 30 days. Never log
callback query strings, tokens, or streaming URLs. Secrets Manager uses its
default recovery window on destroy; remove the GitHub OAuth App separately.

The `HOMEFOLDERS` storage connector persists user home directories. AppStream
application settings persistence is enabled for the stack; users' application
settings are managed by AppStream storage.

## Local validation

From the repository root, test Dev Container initialization without AWS calls:

```sh
python3 -m unittest discover -s .devcontainer -p 'test_*.py' -v
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

From the `terraform` directory:

```sh
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
terraform test
python3 -m unittest discover -s lambda -p 'test_*.py' -v
```

The Terraform mock-provider test requires Terraform 1.7+ and makes no AWS calls.
The Lambda tests require Python 3.10+ and `boto3` locally; the deployed runtime
includes the AWS SDK. They mock AWS and GitHub and do not launch real sessions.
Complete the browser-based checks in [GITHUB-SETUP.md](../GITHUB-SETUP.md) after
deployment.