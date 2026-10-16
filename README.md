# AWS AppStream 開発用踏み台

Terraform で Amazon AppStream 2.0 (WorkSpaces Applications) のネットワーク、Fleet、Stack を作成する IaC です。利用者は Linux デスクトップから Chrome や VS Code を使い、開発環境へ接続する想定です。

## 現在の実装範囲

- 2 つのプライベートサブネットを持つ VPC とリージョナル NAT Gateway
- `stream.standard.xlarge`、1 インスタンスあたり最大 7 セッションのオンデマンド Fleet
- AppStream Stack、ホームフォルダ保存、アプリケーション設定の永続化
- 空きセッション数と稼働率に基づく Fleet のスケーリング
- Codespaces 内の AWS CLI / Terraform と MFA 付き IAM ロールプロファイル

Terraform は公開済みの AppStream イメージを使用します。イメージのビルド・公開処理は含まれていません。AppStream Image Builder で Chrome、VS Code、日本語環境を含むイメージを作成し、そのイメージ名を Terraform に渡してください。Packer の `amazon-ebs` が作成する EC2 AMI は AppStream イメージとして直接利用できません。

GitHub OAuth による利用者認証もまだ接続されていません。README にあった Cognito が GitHub OAuth を受けて SAML アサーションを発行する構成は、そのままでは成立しないため削除しました。実際のサインインには GitHub OAuth と AppStream の間をつなぐ SAML IdP / ブローカー等の選定と実装が別途必要です。現在作成される Stack/Fleet には認証プロバイダーやユーザー割り当ては構成されません。

## 必要な環境

- Terraform 1.6 以降
- AWS Provider 6.x
- 対象リージョンに作成済みの AppStream イメージ
- Codespaces の Secrets:

| 名前 | 用途 |
| --- | --- |
| `GITHUB_ORGANIZATION` | リソース名の末尾と `TF_VAR_github_organization` |
| `AWS_ACCESS_KEY_ID` | MFA を登録した IAM ユーザーのアクセスキー ID |
| `AWS_SECRET_ACCESS_KEY` | 同アクセスキーのシークレット |
| `AWS_ROLE_ARN` | Terraform 実行権限を持つ IAM ロール ARN |
| `AWS_SERIAL_NUMBER` | IAM ユーザーに登録した MFA デバイス ARN |

アクセスキー、ロール、MFA の準備は [AWS-SETUP.md](AWS-SETUP.md) を参照してください。GitHub OAuth App の設定は [GITHUB-SETUP.md](GITHUB-SETUP.md) を参照してください。OAuth の Client ID / Secret は現時点では IaC から参照されません。

## 実行

Codespaces の Secrets を設定して Dev Container を作成すると、`post-create.sh` が `~/.aws` に `terraform-role` プロファイルを作成します。Terraform コマンド実行時に MFA コードを求められたら入力してください。

```sh
cd terraform
terraform init
terraform plan -var='appstream_image_name=YOUR_PUBLISHED_APPSTREAM_IMAGE'
terraform apply -var='appstream_image_name=YOUR_PUBLISHED_APPSTREAM_IMAGE'
```

既定リージョンは `ap-northeast-1` です。変更する場合は `-var='region=YOUR_AWS_REGION'` のように指定してください。組織名に応じ、AWS リソース名には `bustion-${GITHUB_ORGANIZATION}` が含まれます。

Terraform state は現在ローカルに保存されます。Codespace を削除すると state を失う可能性があるため、継続利用や複数人での運用を始める前に、暗号化・アクセス制御された S3 backend と state ロックを構成してください。

## 注意事項

- 最小容量 1、最大容量 4 の設定です。平日朝の事前起動スケジュールは未構成です。
- リージョナル NAT Gateway と AppStream の稼働には継続的な料金が発生します。不要時は `terraform destroy` で削除してください。
- `HOMEFOLDERS` は AppStream のホームフォルダストレージを有効にします。Stack のアプリケーション設定永続化も有効ですが、任意のファイルパスを個別指定する構成は含まれていません。
- 現状、利用者認証と AppStream へのユーザー割り当てが未構成です。認証経路を決めるまでは、エンドユーザーが接続できる状態ではありません。