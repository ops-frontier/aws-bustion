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

このロールには、Terraform が作成・更新・削除する VPC / EC2 ネットワーク、AppStream、Application Auto Scaling、CloudWatch Alarm の操作権限が必要です。また AppStream のサービスリンクロールをまだ作成していないアカウントでは、`iam:CreateServiceLinkedRole` を `appstream.amazonaws.com` に対して許可してください。権限は対象リージョン・リソースに絞り、必要な読み取り権限と削除権限も含めてください。広範な管理ポリシーを常用するのではなく、組織の IAM 管理者が実際の Terraform plan に必要な権限を確認してください。

GitHub 認証サービス用に API Gateway v2、Lambda、DynamoDB、Secrets Manager、CloudWatch Logs と Lambda 実行ロール / インラインポリシーの作成・更新・削除権限、およびそのロールを Lambda に渡す `iam:PassRole` も必要です。OAuth 資格情報を登録する作業者には、Terraform が作成した Secret に限定した `secretsmanager:PutSecretValue` を許可してください。Secret の値は Terraform で管理しません。

IAM ユーザー側には、作成したロール ARN に対する `sts:AssumeRole` を許可します。信頼ポリシーとユーザーの許可ポリシーの両方が必要です。

## 3. Codespaces Secrets

GitHub リポジトリの **Settings → Secrets and variables → Codespaces** で、次の Secrets を登録します。

| Secret | 設定値 |
| --- | --- |
| `AWS_ACCESS_KEY_ID` | IAM ユーザーのアクセスキー ID |
| `AWS_SECRET_ACCESS_KEY` | IAM ユーザーの Secret access key |
| `AWS_ROLE_ARN` | Terraform 実行ロール ARN |
| `AWS_SERIAL_NUMBER` | MFA デバイス ARN（例: `arn:aws:iam::123456789012:mfa/iac-operator`） |
| `GITHUB_ORGANIZATION` | リソース名に使用する GitHub 組織名 |
| `APPSTREAM_IMAGE_NAME` | 対象リージョンに公開済みの AppStream イメージ名 |

Codespace を再作成すると Dev Container の post-create スクリプトが AWS CLI の `base` と `terraform-role` プロファイルを設定します。値は Codespaces Secrets から環境変数として渡され、AWS 設定ファイルはユーザーのみ読み書きできる権限で作成されます。

同スクリプトが `GITHUB_ORGANIZATION` と `APPSTREAM_IMAGE_NAME` を `TF_VAR_github_organization` と `TF_VAR_appstream_image_name` に転記し、新しい Bash ターミナルで読み込むよう設定します。Codespaces Secrets には大文字の名前だけを登録してください。

## 4. 認証の確認

Dev Container のターミナルで次を実行します。AWS CLI が MFA コードを尋ねたら認証アプリ等に表示された現在のコードを入力してください。

```sh
aws sts get-caller-identity --profile terraform-role
```

返された ARN が作成した実行ロールであることを確認します。ロールセッションの有効期限後は再度 MFA コードの入力が必要です。Terraform provider も `terraform-role` を使用します。

## 5. 失効・ローテーション

アクセスキーの漏えい、担当者の変更、不要化が発生したら IAM でキーを無効化または削除し、必要に応じて新しいキーを発行して Codespaces Secrets を更新してください。MFA デバイスやロールの信頼先を変更した場合も、関連する Secret と IAM ポリシーを更新します。