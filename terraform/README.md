# Terraform

This configuration creates the private VPC, regional NAT gateway, AppStream
fleet and stack, capacity scaling alarms, and a GitHub OAuth launch service.
Resource names include
`bustion-${GITHUB_ORGANIZATION}`.

## Prerequisites

- Terraform 1.6 or later, AWS provider 6.x, and Archive provider 2.x (>= 2.7).
- Codespaces secrets from the root README, including `GITHUB_ORGANIZATION`
  and `APPSTREAM_IMAGE_NAME`.
- An already published AppStream image containing the required Linux desktop
  applications. Set its name in the uppercase `APPSTREAM_IMAGE_NAME` secret.
- AWS permissions to create VPC, AppStream, IAM service-linked roles,
  Application Auto Scaling, CloudWatch, API Gateway v2, Lambda, DynamoDB,
  Secrets Manager, and the Lambda execution role/policy, including `iam:PassRole`.
  Credential registration also requires `secretsmanager:PutSecretValue`.

The workspace creates a role profile using the supplied source access key.
Terraform and AWS CLI operations using that profile prompt for the MFA code.

The post-create script maps `GITHUB_ORGANIZATION` and `APPSTREAM_IMAGE_NAME`
to `TF_VAR_github_organization` and `TF_VAR_appstream_image_name`. It saves
these exports in `~/.config/aws-bustion/terraform-env.sh` and loads them through
`~/.bashrc` for new Bash terminals. No lowercase Codespaces secrets or
`-var` arguments are needed.

```sh
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