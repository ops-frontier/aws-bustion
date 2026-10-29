# Terraform

This configuration creates the private VPC, regional NAT gateway, AppStream
fleet and stack, capacity scaling alarms, a Managed Microsoft AD directory
registered with WorkSpaces Personal, and a shared GitHub OAuth launch service.
It also provisions one private `t4g.micro` (1 GiB RAM) FreeRADIUS server and enables
Managed Microsoft AD RADIUS MFA. Users enroll a TOTP authenticator through the
portal before connecting with their AD password and OTP.
Resource names include
`bustion-${BUSTION_GITHUB_ORGANIZATION}`.

## Linux fleet capacity

The `ALWAYS_ON` fleet uses one Linux instance per session. The multi-session-only
`max_sessions_per_instance` argument is intentionally omitted. The existing
Linux image and `stream.standard.xlarge` size remain unchanged. Windows uses
WorkSpaces Personal, not this fleet; see [WINDOWS-AWP.md](../WINDOWS-AWP.md#実装と導入).
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

Application Auto Scaling manages desired capacity and, after creation, the
minimum capacity. After GitHub membership verification, the launch Lambda also
requests `DesiredInstances = 1` after Linux is selected if desired capacity and in-use capacity are both
zero. Terraform ignores `desired_instances` and the scalable target's
`min_capacity` so a later apply does not override scaling or the schedule.

This explicit wake-up does not depend on a rejected AppStream session or
CloudWatch metric delivery. While there are neither active sessions nor available
instances, the Lambda returns a waiting page (HTTP 503, `Retry-After: 20`) instead
of creating a short-lived streaming URL. The page returns to `/linux` every 20
seconds, rechecking capacity within the authenticated browser session.
Users can also select "Check again"; refreshing a consumed `/callback` or
pressing AppStream Retry is not the recovery path. Provisioning usually takes
10-20 minutes. A nonzero desired capacity prevents repeated wake-up requests.
Existing session reconnection remains unchanged even without spare capacity.
Missing or stopped fleets and AWS API failures return errors, not a waiting page;
the Lambda does not start administratively stopped fleets.

The Lambda requires `appstream:DescribeFleets` with `Resource = "*"` (this action
does not support resource-level permissions), and `appstream:UpdateFleet` scoped
to this fleet. Apply the Terraform changes to deploy both code and permissions.

- Scale out by one when `InUseCapacity >= 1` and `AvailableCapacity <= 1`, or
  when `InsufficientCapacityError >= 1` while `PendingCapacity == 0`, evaluated
  with metric math over one one-minute period; the scale-out cooldown is 120
  seconds. The capacity-error branch remains a fallback for rejected launches;
  normal login to an empty fleet uses the explicit Lambda wake-up above.
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

- Terraform 1.7 or later, AWS provider 6.x, Archive provider 2.x (>= 2.7),
  and Random provider 3.x (>= 3.7).
- Python 3.10+ and AWS CLI v2 (installed by the Dev Container).
- Codespaces secrets from the root README, including `BUSTION_GITHUB_ORGANIZATION`
  and `APPSTREAM_IMAGE_NAME`.
- An already published AppStream image containing the required Linux desktop
  applications. Set its name in the uppercase `APPSTREAM_IMAGE_NAME` secret.
- An existing `AmazonAppStreamServiceAccess` service role (see below), which
  this configuration reads but does not manage.
- An existing `workspaces_DefaultRole` with the WorkSpaces service and self-service
  policies, trusted by `workspaces.amazonaws.com`; see [AWS-SETUP.md](../AWS-SETUP.md#workspaces-personal-の共通サービスロール).
- AWS permissions to create VPC, AppStream, `iam:GetRole` on the AppStream
  service role,
  Application Auto Scaling, CloudWatch, API Gateway v2, Lambda, DynamoDB,
  Secrets Manager, and the Lambda execution role/policy, including `iam:PassRole`.
  Application Auto Scaling may also require `iam:CreateServiceLinkedRole` for
  `appstream.application-autoscaling.amazonaws.com` on first use.
  Credential registration also requires `secretsmanager:PutSecretValue`.
- Directory Service create/delete and data-access configuration, and WorkSpaces
  directory registration/deregistration permissions.
- EC2 instance/profile/security group management, IAM role passing, the public
  Ubuntu AMI SSM parameter, KMS key/alias management, and Directory Service
  RADIUS enable/update/disable permissions, plus SSM `SendCommand`,
  `DescribeInstanceInformation`, `ListCommandInvocations`, `GetCommandInvocation`
  and `CancelCommand` permissions. The shared RADIUS secret is
  generated by Terraform and is present in state; unlike the OAuth secret,
  its Secrets Manager version is Terraform-managed.

Before every plan/test/apply from a fresh checkout, build the QR Lambda layer:

```sh
bash ../scripts/build_qr_layer.sh
```

The script uses Python/pip and the pinned dependency in `lambda/requirements.txt`.
The pure-Python layer is built under `.terraform/qr-layer/`; no build artifacts
are committed. Rebuild after deleting `.terraform/` or changing the requirements.

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

The public HTTP API exposes `GET /`, `/login`, `/callback`, `/session-ended`,
`/desktop`, `/linux`, `/windows`, and `POST /linux` and `/windows`.
All Lambda HTML pages share a header with a left-aligned ops-frontier logo
and the horizontally centered title "ゼロトラスト仮想化端末サービス".
On narrow screens the title moves below the logo to avoid overlap.
The favicon and logo are served without authentication at `GET /favicon.ico`
and `GET /ops-frontier-logo.svg`. Both assets in `lambda/` are packaged with
the Python handler; tests and bytecode caches are excluded from the ZIP.
Apply Terraform to deploy the HTML, assets, and API routes together.
The Python 3.13 Lambda runs outside the VPC and uses GitHub OAuth with PKCE
and browser-bound, single-use state (10-minute expiry). DynamoDB conditional
deletion enforces expiry and prevents replay independently of TTL cleanup.
Only active members of `github_organization` may obtain a browser session.
OAuth redirects to `/desktop` to choose セキュアブラウザ (AppStream Linux) or
パーソナルデスクトップ (WorkSpaces Personal Windows). See the
[service descriptions](../README.md#端末の呼び名と用途) for usage and connection methods. Sessions
expire after one hour, store only a hash of the random cookie token, and use
CSRF tokens plus same-origin POST validation. Tokens from GitHub are never saved.
The HTML Content Security Policy restricts form submissions to `form-action 'self'`.
`POST /linux` validates the session, origin, and CSRF token, records the Linux
selection, and returns an HTTP 200 handoff page with an immediate meta refresh
and a fallback link to `/linux`. This ends the form submission before a separate
GET navigation checks capacity and redirects to AppStream. Chromium checks the
entire redirect chain of form submissions, including AppStream's downstream
hosts; the handoff avoids expanding the form-action allowlist.
The POST origin and CSRF checks remain unchanged. After deploying this change,
reload `/desktop` before trying again; an already open page retains its old policy.

After applying, follow [GITHUB-SETUP.md](../GITHUB-SETUP.md) to register an OAuth
App with the callback output and populate the secret output with
`{"client_id":"...","client_secret":"..."}`. Terraform manages only the secret
container, never its value. Sign-in returns 503 until credentials are configured.
No Cognito, OIDC wrapper, SAML federation, or AppStream user-pool assignments
are required.

Lambda verifies the authenticated user's organization membership at OAuth
login. Selecting Linux calls `CreateStreamingURL` with a 60-second validity and stable
`gh-<numeric GitHub ID>` user ID, and redirects the browser to that URL.
The existing `APP` stream view is unchanged; no `ApplicationId` is specified.
GitHub tokens are not persisted, and removing organization membership does
not terminate existing AppStream sessions.

The execution role reads the OAuth secret, manages state/sessions and identity
bindings in the dedicated DynamoDB tables, writes its logs, inspects/wakes the
Linux fleet, and creates its streaming URLs. It can also create/describe users
and reset initial passwords only in the new AD directory, and create/tag
WorkSpaces. It cannot delete AD users or WorkSpaces. Lambda logs are retained for 30 days. Never log
callback query strings, tokens, or streaming URLs. Secrets Manager uses its
default recovery window on destroy; remove the GitHub OAuth App separately.

## WorkSpaces RADIUS MFA

The password registration form includes a locally generated inline SVG QR code,
a manual enrollment key, and a six-digit confirmation field. No third-party QR
service is called. A pending seed expires after ten minutes; confirmation activates
it before AD/WorkSpace provisioning. Existing AD users enroll without resetting
their password. Active seeds are never displayed or replaced through the portal.

TOTP uses SHA-1, six digits, 30-second steps and a +/- one-step clock window.
Both enrollment and RADIUS share atomic DynamoDB replay protection and a limit
of five attempts per account per five-minute window. A consumed code cannot
be reused, including the enrollment confirmation code; wait for the next code
before the first client sign-in. These limits can also be abused to temporarily
deny a user access. Maintain AD lockout policies and endpoint protections;
TOTP is not phishing-resistant.

Seeds are envelope-encrypted using a rotating KMS key and a login-bound
encryption context, stored in a PITR-enabled table without TTL. The RADIUS role
can read/update that table and decrypt seeds, but cannot enroll seeds or reset
AD passwords. Restrict administrator access to this table, KMS and its backups.
The RADIUS shared secret is fetched from Secrets Manager at EC2 provisioning,
written root-owned with `0640` permissions, and is not embedded in user data.

FreeRADIUS runs on Ubuntu 24.04 ARM64 with 1 GiB RAM, encrypted gp3 storage, IMDSv2 and no
public IP or SSH ingress. Only the directory security group may reach UDP 1812;
FreeRADIUS additionally accepts only the directory controller IPs. PAP carries
the OTP, not the AD password, between Directory Service and RADIUS. This is
private RADIUS/UDP, not TLS; never publish it on the Internet. Message-Authenticator
is required and included in replies. Do not disable it to work around a client
compatibility problem; verify the real Directory Service integration before
production rollout.

The instance installs distribution packages via NAT, uses Amazon Time Sync,
and is managed via SSM. Before enabling directory MFA, Terraform waits up to
20 minutes for an SSM Run Command to successfully complete `cloud-init status --wait`,
an active FreeRADIUS service, and `freeradius -C`. EC2's running state alone
does not mean its packages and RADIUS configuration are ready. Failed bootstrap
or an unreachable SSM agent blocks activation with an explicit error.
The readiness provisioner uses the local AWS CLI and `terraform-role` profile.
Keep `wait_for_radius_ready = true` (the default) for real deployments; the
offline mock tests disable it to avoid making live AWS calls.
The PAP-only configuration disables the distribution's EAP module, which
otherwise fails startup because this server has no EAP authentication section.
Directory Service does not return the shared secret on reads. Terraform ignores
that unreadable field's refresh drift, but replacing `random_password.radius`
forces replacement of the directory RADIUS settings as well as the host, so
secret rotation still updates both sides.
User-data changes replace it, interrupting new logins
during bootstrap. The EC2 status alarm has no notification destination by
default; connect it to your monitoring. It does not detect every process or
dependency failure. Verify FreeRADIUS health and patch/reprovision the host
regularly. One server is intentionally a single point of failure; dependency
failures reject authentication without a password-only fallback.

See [rollout, recovery, and real-client checks](../WINDOWS-AWP.md#radius-mfa-の導入と運用).

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
bash ../scripts/build_qr_layer.sh
terraform validate
terraform test
python3 -m pip install -r lambda/requirements.txt
python3 -m unittest discover -s lambda -p 'test_*.py' -v
python3 -m unittest discover -s radius -p 'test_*.py' -v
```

The Terraform mock-provider test requires Terraform 1.7+ and makes no AWS calls.
The Lambda tests require Python 3.10+ and `boto3` locally; the deployed runtime
includes the AWS SDK. The QR library is deployed as a Lambda layer.
Tests mock AWS and GitHub and do not launch real sessions.
Complete the browser-based checks in [GITHUB-SETUP.md](../GITHUB-SETUP.md) and
[WINDOWS-AWP.md](../WINDOWS-AWP.md#実機確認) after deployment.