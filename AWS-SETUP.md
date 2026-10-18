# AWS アクセスキー、IAM ロール、MFA の準備

このリポジトリの Dev Container は IAM ユーザーのアクセスキーを元に、MFA を使って `AWS_ROLE_ARN` のロールを引き受けます。AWS アカウント管理者が以下を準備してください。アクセスキーは開発者個人に発行し、共有しないでください。

## 1. IAM ユーザーと MFA

1. AWS IAM で Terraform 実行者用の IAM ユーザーを作成します。組織の認証ポリシーに適合する場合は、長期アクセスキーを発行できる専用ユーザーにしてください。
2. IAM ユーザーの Security credentials から MFA デバイスを登録します。認証アプリまたはハードウェア MFA を使用します。
3. 同じ画面からアクセスキーを作成し、表示された Access key ID と Secret access key を安全に保管します。Secret access key は作成時にしか表示されません。

組織で長期アクセスキーが禁止されている場合、この Dev Container の認証方式は利用できません。AWS IAM Identity Center 等に対応する認証方式へ変更してから利用してください。

## 2. Terraform 実行ロール

IAM でロールを作成し、信頼ポリシーの Principal を前項の IAM ユーザー ARN に限定します。MFA なしでの引き受けを防ぐため、信頼ポリシーに次の条件を含めてください。

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::123456789012:user/iac-operator" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "Bool": { "aws:MultiFactorAuthPresent": "true" }
    }
  }]
}
```

`Principal` は実際のアカウント ID と IAM ユーザー ARN に置き換えてください。作成後、ロール ARN を控えます。

このロールには、Terraform が作成・更新・削除する VPC / EC2 ネットワーク、AppStream、Application Auto Scaling、CloudWatch Alarm の操作権限が必要です。AppStream のサービスロール `AmazonAppStreamServiceAccess` は他の AppStream 環境と共用するため、Terraform では管理しません。Terraform は存在とパスを確認するだけなので、このロールに対しては `iam:GetRole` だけが必要で、作成・削除・ポリシーのアタッチ権限は不要です。`terraform destroy` でもこのロールは削除されません。これはサービスリンクロールではないため、`appstream.amazonaws.com` を指定した `iam:CreateServiceLinkedRole` では作成できません。詳細は [Terraform の説明](terraform/README.md#appstream-service-role) を参照してください。一方、Application Auto Scaling の初回利用では `appstream.application-autoscaling.amazonaws.com` に対する `iam:CreateServiceLinkedRole` が必要になる場合があります。権限は対象リージョン・リソースに絞り、必要な読み取り権限と削除権限も含めてください。広範な管理ポリシーを常用するのではなく、組織の IAM 管理者が実際の Terraform plan に必要な権限を確認してください。

`AmazonAppStreamServiceAccess` は、Terraform の実行前に IAM パス `/service-role/` に作成しておく必要があります（AppStream コンソールでの初回設定や、アカウント共通の IAM 管理で作成）。信頼ポリシーは `appstream.amazonaws.com`、アタッチするポリシーは AWS 管理ポリシー `arn:aws:iam::aws:policy/service-role/AmazonAppStreamServiceAccess` です。ロール ARN は `arn:aws:iam::<account-id>:role/service-role/AmazonAppStreamServiceAccess` になります。ロールが存在しない場合や、パスが `/service-role/` 以外の場合は、Terraform の plan がエラーで止まります。パスの修正にはロールの再作成が必要で、共用ロールのため他の AppStream 環境への影響を確認してください。[InvalidRoleException の復旧手順](terraform/README.md#recovering-from-invalidroleexception) を参照してください。

GitHub 認証サービス用に API Gateway v2、Lambda、DynamoDB、Secrets Manager、CloudWatch Logs と Lambda 実行ロール / インラインポリシーの作成・更新・削除権限、およびそのロールを Lambda に渡す `iam:PassRole` も必要です。OAuth 資格情報を登録する作業者には、Terraform が作成した Secret に限定した `secretsmanager:PutSecretValue` を許可してください。Secret の値は Terraform で管理しません。

IAM ユーザー側には、作成したロール ARN に対する `sts:AssumeRole` を許可します。信頼ポリシーとユーザーの許可ポリシーの両方が必要です。

## 3. Codespaces Secrets

GitHub リポジトリの **Settings → Secrets and variables → Codespaces** で、次の Secrets を登録します。

| Secret | 設定値 |
| --- | --- |
| `AWS_ACCESS_KEY_ID` | IAM ユーザーのアクセスキー ID |
| `AWS_SECRET_ACCESS_KEY` | IAM ユーザーの Secret access key |
| `AWS_ROLE_ARN` | Terraform 実行ロールの完全な ARN（例: `arn:aws:iam::123456789012:role/terraform-admin`）。ロール名のみは不可 |
| `AWS_SERIAL_NUMBER` | MFA デバイス ARN（例: `arn:aws:iam::123456789012:mfa/iac-operator`） |
| `BUSTION_GITHUB_ORGANIZATION` | リソース名に使用する GitHub 組織名 |
| `APPSTREAM_IMAGE_NAME` | 対象リージョンに公開済みの AppStream イメージ名 |

Codespace を再作成すると Dev Container の post-create スクリプトが元のアクセスキーを `~/.aws/credentials` の `base` に保存します。`~/.aws/config` の `terraform-mfa` にロール ARN・`source_profile = base`・MFA デバイス ARN を設定し、`terraform-role` にはリージョンのみを設定します。値は Codespaces Secrets から環境変数として渡され、AWS 設定ファイルはユーザーのみ読み書きできる権限で作成されます。初期化時には MFA 入力を求めず、ロールの一時認証情報はまだありません。

同スクリプトが `BUSTION_GITHUB_ORGANIZATION` と `APPSTREAM_IMAGE_NAME` を `TF_VAR_github_organization` と `TF_VAR_appstream_image_name` に転記し、新しい Bash ターミナルで読み込むよう設定します。Codespaces Secrets には大文字の名前だけを登録してください。

## 4. 認証の確認

Dev Container のターミナルで、リポジトリ直下から次を実行します。Python スクリプトが標準出力に `MFA OTP (6 digits):` と表示したら、認証アプリ等に表示された現在のコードを標準入力から入力してください。Python 3 と AWS CLI は Dev Container にインストールされます。

```sh
python3 scripts/login.py
aws sts get-caller-identity --profile terraform-role
```

スクリプトは `terraform-mfa` の設定を読み取り、`base` の認証情報で AWS STS AssumeRole を呼び出します。取得した 3 つの一時認証情報（アクセスキー ID・シークレットキー・セッショントークン）を `~/.aws/credentials` の `terraform-role` に保存します。他のプロファイルは保持し、ファイルは権限 `600` で原子的に更新します。AWS API エラー、設定不備、不正な OTP 等はエラー終了し、既存の認証情報を変更しません。OTP や認証情報をログ・チャット・Git に保存しないでください。

返された ARN が作成した実行ロールであることを確認します。Terraform provider と以後の AWS CLI 操作は `terraform-role` を使用し、追加の MFA 入力はありません。Terraform provider は対話的な MFA 入力に対応しないため、`terraform-role` に `role_arn`・`source_profile`・`mfa_serial` を設定しないでください。

スクリプトに表示される有効期限（既定で 1 時間）を過ぎたら `python3 scripts/login.py` を再実行します。`post-create.sh` を再実行した場合も一時認証情報が削除されるため、再ログインしてください。AWS の設定パスを変更する場合、スクリプトは `AWS_CONFIG_FILE` と `AWS_SHARED_CREDENTIALS_FILE` に対応しますが、Terraform provider の `shared_config_files` / `shared_credentials_files` も同じパスに変更する必要があります。

## 5. 失効・ローテーション

アクセスキーの漏えい、担当者の変更、不要化が発生したら IAM でキーを無効化または削除し、必要に応じて新しいキーを発行して Codespaces Secrets を更新してください。MFA デバイスやロールの信頼先を変更した場合も、関連する Secret と IAM ポリシーを更新します。