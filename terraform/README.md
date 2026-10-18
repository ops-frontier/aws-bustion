# Terraform

This configuration creates the private VPC, regional NAT gateway, AppStream
fleet and stack, capacity scaling alarms, and a GitHub OAuth launch service.
Resource names include
`bustion-${BUSTION_GITHUB_ORGANIZATION}`.

## Linux fleet capacity

The `ALWAYS_ON` fleet uses one Linux instance per session. The multi-session-only
`max_sessions_per_instance` argument is intentionally omitted. Windows support
is deferred; the existing Linux image and `stream.standard.xlarge` size remain.
Always-On spare instances are already running, so sessions start without the
On-Demand instance start-up delay, but idle instances incur running charges.

Fleet type cannot change on a schedule, so business hours are implemented with
Application Auto Scaling scheduled actions on the minimum capacity. By default
the minimum is `min_capacity` (1) from 08:30 and 0 from 17:00 every day in
`Asia/Tokyo` (`business_hours_start_schedule`, `business_hours_end_schedule`,
`business_hours_timezone`). Because an empty Always-On fleet can take several
minutes or more to provision its first instance, the minimum is raised 30
minutes early so a running instance is ready by 09:00. Widen the lead time if
provisioning is observed to take longer. Maximum capacity is 22 and initial
capacity is 1, allowing 20 concurrent users plus spare capacity.

Application Auto Scaling owns desired capacity and, after creation, the
minimum capacity; Terraform ignores `desired_instances` and the scalable
target's `min_capacity` so a later apply does not override the schedule.

- Scale out by one when `InUseCapacity >= 1` and `AvailableCapacity <= 1`, or
  when `InsufficientCapacityError >= 1` while `PendingCapacity == 0`, evaluated
  with metric math over one one-minute period; the scale-out cooldown is 120
  seconds. The capacity-error branch lets a rejected launch outside business
  hours start one instance; the user retries after it is provisioned.
- Scale in by one when `AvailableCapacity >= 3`, or `InUseCapacity == 0` and
  `AvailableCapacity >= 2`, for five consecutive one-minute periods.
- Scale in by one when `InUseCapacity == 0` and `AvailableCapacity >= 1` for 30
  consecutive one-minute periods. During business hours the scheduled minimum
  keeps the last instance; afterwards the fleet drains to zero.
- The scale-in cooldown is 300 seconds. Scaling never goes below the current
  scheduled minimum. Metric delivery, provisioning delays, and concurrent
  arrivals can still temporarily exhaust capacity.

Scale-in removes unused capacity without terminating active sessions. These
waits are not user inactivity timeouts; session idle/disconnect timeout
settings are unchanged. Ensure the regional AppStream instance quota permits
the configured maximum.

Changing `fleet_type` replaces the fleet under the same name. The stack
association, scalable target, scheduled actions, and scaling policies use
`replace_triggered_by` so they are recreated around the new fleet. Apply such a
change when no sessions are active.

## Prerequisites

- Terraform 1.7 or later, AWS provider 6.x, and Archive provider 2.x (>= 2.7).
- Python 3.10+ and AWS CLI v2 (installed by the Dev Container).
- Codespaces secrets from the root README, including `BUSTION_GITHUB_ORGANIZATION`
  and `APPSTREAM_IMAGE_NAME`.
- An already published AppStream image containing the required Linux desktop
  applications. Set its name in the uppercase `APPSTREAM_IMAGE_NAME` secret.
- An existing `AmazonAppStreamServiceAccess` service role (see below), which
  this configuration reads but does not manage.
- AWS permissions to create VPC, AppStream, `iam:GetRole` on the AppStream
  service role,
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
It is not a service-linked role: calling `CreateServiceLinkedRole` for
`appstream.amazonaws.com` fails with `Cannot find Service Linked Role template`.
The fleet's optional `iam_role_arn` is for streaming instance credentials and
must not be used for this service role.

This role is shared account-wide with other AppStream deployments, so this
configuration **does not manage it**. Terraform only reads it with the
`aws_iam_role` data source, fails the plan unless its path is `/service-role/`,
and creates the fleet and stack after that lookup. `terraform destroy` never
deletes the role or its policy attachment. Create and maintain them outside
this configuration (for example, with the AppStream console or the account's
shared IAM configuration).

Earlier versions of this configuration created the role. The `removed` blocks
in `main.tf` (`lifecycle { destroy = false }`, Terraform 1.7+) drop
`aws_iam_role.appstream` and `aws_iam_role_policy_attachment.appstream` from an
existing state without deleting them; the next plan reports that they
"will no longer be managed by Terraform, but will not be destroyed". Keep the
`removed` blocks until every state using this configuration has been applied.
Do not run `terraform import` for these resources.

Before applying in a new account, check that the role exists:

```sh
aws iam get-role --role-name AmazonAppStreamServiceAccess \
  --query 'Role.{Arn:Arn,Path:Path,Trust:AssumeRolePolicyDocument}' \
  --profile terraform-role
aws iam list-attached-role-policies \
  --role-name AmazonAppStreamServiceAccess --profile terraform-role
```

If it is absent (`NoSuchEntity`), have the account administrator create it at
path `/service-role/` with the trust policy and managed policy above. Terraform
needs only `iam:GetRole` on it.

### Recovering from `InvalidRoleException`

If `CreateFleet` reports that
`arn:aws:iam::<account-id>:role/service-role/AmazonAppStreamServiceAccess`
is invalid, check the role with the commands above. A role at
`arn:aws:iam::<account-id>:role/AmazonAppStreamServiceAccess` (path `/`)
does not match, and this configuration's plan fails before creating the fleet.
IAM paths cannot be updated in place; correcting the path requires the account
administrator to recreate the shared role, which affects other AppStream
deployments, so coordinate a maintenance window.

If the path, trust policy, and attachment are correct but the error persists
immediately after the role was created or changed, allow a few minutes for IAM
propagation and rerun `terraform plan` and `terraform apply`. AWS provider
6.69.1's Fleet creation handler retries `InvalidRoleException` only when the
message contains `encountered an error because your IAM role`; the message
`encountered an error because your service role` fails without that retry. See
the [provider implementation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.69.1/internal/service/appstream/fleet.go).
If the error continues, have the account administrator check the role's
effective permissions, including any permissions boundary or organization
policies. Do not set the fleet's `iam_role_arn` to work around this error.

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

Home folders are disabled: the stack has no storage connectors, so nothing is
persisted between sessions. The AWS provider does not apply storage connector
removal to an existing stack, so `terraform_data.disable_home_folders` runs
`aws appstream update-stack --attributes-to-delete STORAGE_CONNECTORS` with the
`terraform-role` profile whenever the stack is created or replaced. Set
`disable_home_folders_with_cli = false` to skip it (the tests do). Existing home
folder data in the AppStream S3 bucket is not deleted.
Application settings persistence is unsupported for Linux-based
stacks and is explicitly disabled, including on existing stacks during updates.
Enabling it causes `IncompatibleImageException` when associating the Linux fleet
with the stack; rebuilding the Linux image does not enable this feature.
See [AWS application settings persistence documentation](https://docs.aws.amazon.com/appstream2/latest/developerguide/app-settings-persistence.html)
and [home folders](https://docs.aws.amazon.com/appstream2/latest/developerguide/home-folders.html).

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