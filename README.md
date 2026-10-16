# AWS AppStream 開発用踏み台

Terraform で Amazon AppStream 2.0 (WorkSpaces Applications) のネットワーク、Fleet、Stack を作成する IaC です。利用者は Linux デスクトップから Chrome や VS Code を使い、開発環境へ接続する想定です。

## 現在の実装範囲

- 2 つのプライベートサブネットを持つ VPC とリージョナル NAT Gateway
- `stream.standard.xlarge`、1 インスタンスあたり最大 7 セッションのオンデマンド Fleet
- AppStream Stack、ホームフォルダ保存、アプリケーション設定の永続化
- 空きセッション数と稼働率に基づく Fleet のスケーリング
- API Gateway + Lambda による GitHub OAuth 認証、Organization メンバーシップ確認、AppStream 起動
- Secrets Manager による OAuth 資格情報管理、DynamoDB による一回限りの OAuth state 管理
- Codespaces 内の AWS CLI / Terraform と MFA 付き IAM ロールプロファイル

Terraform は公開済みの AppStream イメージを使用します。イメージのビルド・公開処理は含まれていません。AppStream Image Builder で Chrome、VS Code、日本語環境を含むイメージを作成し、そのイメージ名を Codespaces Secret の `APPSTREAM_IMAGE_NAME` に設定してください。Packer の `amazon-ebs` が作成する EC2 AMI は AppStream イメージとして直接利用できません。

## 認証・起動の流れ

```text
利用者 → API Gateway /login → Lambda → GitHub OAuth
       → API Gateway /callback → Lambda
       → GitHub API で本人と Organization の active メンバーシップを確認
       → AppStream CreateStreamingURL → AppStream にリダイレクト
```

Cognito、OIDC ラッパー、SAML IdP は使用しません。`github_organization` に指定した Organization の有効なメンバー全員（非公開メンバーを含む）を許可し、招待待ち・非メンバーは拒否します。`read:org` scope のユーザートークンで本人のメンバーシップを確認するため、管理者の PAT は不要です。

OAuth は PKCE（S256）と Secure / HttpOnly / SameSite=Lax Cookie に紐づく state で保護します。state は 10 分間有効で、DynamoDB の条件付き削除により一回だけ使用できます。GitHub のアクセストークンは Lambda の処理中だけ使用し、DB やログには保存しません。AppStream の UserId は変更可能な GitHub ログイン名ではなく `gh-<GitHub の数値 ID>` を使用し、ホームフォルダ等の識別子を安定させます。

## 必要な環境

- Terraform 1.6 以降
- AWS Provider 6.x
- Archive Provider 2.7 以降（2.x、Lambda の ZIP 作成に使用）
- 対象リージョンに作成済みの AppStream イメージ
- Codespaces の Secrets:

| 名前 | 用途 |
| --- | --- |
| `GITHUB_ORGANIZATION` | 利用を許可する GitHub Organization、リソース名の末尾、`TF_VAR_github_organization` |
| `APPSTREAM_IMAGE_NAME` | 公開済み AppStream イメージ名、`TF_VAR_appstream_image_name` |
| `AWS_ACCESS_KEY_ID` | MFA を登録した IAM ユーザーのアクセスキー ID |
| `AWS_SECRET_ACCESS_KEY` | 同アクセスキーのシークレット |
| `AWS_ROLE_ARN` | Terraform 実行権限を持つ IAM ロール ARN |
| `AWS_SERIAL_NUMBER` | IAM ユーザーに登録した MFA デバイス ARN |

アクセスキー、ロール、MFA の準備は [AWS-SETUP.md](AWS-SETUP.md) を参照してください。GitHub OAuth App の設定は [GITHUB-SETUP.md](GITHUB-SETUP.md) を参照してください。OAuth の Client ID / Secret は apply 後に Secrets Manager に登録します。Terraform 変数や Codespaces Secrets には渡しません。

## 実行

Codespaces の Secrets を設定して Dev Container を作成すると、`post-create.sh` が `~/.aws` に `terraform-role` プロファイルを作成します。Terraform コマンド実行時に MFA コードを求められたら入力してください。

同スクリプトが大文字の `GITHUB_ORGANIZATION` / `APPSTREAM_IMAGE_NAME` を、それぞれ `TF_VAR_github_organization` / `TF_VAR_appstream_image_name` に転記します。設定は `~/.config/aws-bustion/terraform-env.sh` に保存し、`~/.bashrc` から読み込むため、新しく開いた Bash ターミナルで有効です。イメージ名を `-var` で渡す必要はありません。

```sh
cd terraform
terraform init
terraform plan
terraform apply
terraform output -raw appstream_login_url
terraform output -raw github_oauth_callback_url
terraform output -raw github_oauth_secret_arn
```

Secret を変更した場合は Codespace を再起動して環境変数を反映し、リポジトリ直下で `bash .devcontainer/post-create.sh` を再実行してください。その後、新しい Bash ターミナルを開くか、既存のターミナルで `source "$HOME/.config/aws-bustion/terraform-env.sh"` を実行します。

apply 後、[GITHUB-SETUP.md](GITHUB-SETUP.md) に従い、出力された callback URL で GitHub OAuth App を作成し、出力された ARN の Secret に資格情報を登録してください。**登録するまではログインできません（503 応答）。** Secret の値は Terraform で作成・取得しないため、Terraform state に入りません。登録後、`appstream_login_url` をブラウザーで開きます。Secret の更新だけなら再 apply は不要です。

既定リージョンは `ap-northeast-1` です。変更する場合は `-var='region=YOUR_AWS_REGION'` のように指定してください。組織名に応じ、AWS リソース名には `bustion-${GITHUB_ORGANIZATION}` が含まれます。

Terraform state は現在ローカルに保存されます。Codespace を削除すると state を失う可能性があるため、継続利用や複数人での運用を始める前に、暗号化・アクセス制御された S3 backend と state ロックを構成してください。

## 注意事項

- 最小容量 1、最大容量 4 の設定です。平日朝の事前起動スケジュールは未構成です。
- リージョナル NAT Gateway と AppStream の稼働には継続的な料金が発生します。不要時は `terraform destroy` で削除してください。
- 認証用の API Gateway、Lambda、DynamoDB、Secrets Manager、CloudWatch Logs にも料金が発生します。Lambda は VPC に接続せず、GitHub と AWS API にアクセスします。
- `HOMEFOLDERS` は AppStream のホームフォルダストレージを有効にします。Stack のアプリケーション設定永続化も有効ですが、任意のファイルパスを個別指定する構成は含まれていません。
- 起動 URL の有効期限は 60 秒です。URL 自体がアクセス権になるため、共有・ログ保存しないでください。この期限はセッションの最大利用時間ではありません。
- Organization の利用権限は起動時に毎回確認します。メンバーを削除しても既存の AppStream セッションは即時終了しません。必要な場合は管理者がセッションを終了してください。
- Fleet が停止中・容量不足・起動準備中の場合は接続できません。Fleet の稼働と公開イメージ内のアプリケーションを事前に確認してください。既存の `APP` stream view を維持し、起動 URL に特定の ApplicationId は指定しません。
- API Gateway は公開 HTTPS エンドポイントです。Organization の OAuth App 制限・SSO ポリシーによっては管理者承認が必要です。詳細は [GITHUB-SETUP.md](GITHUB-SETUP.md) を参照してください。
- Secrets Manager の Secret は destroy 時に既定の復旧期間付きで削除予約されます。GitHub OAuth App の削除・失効は別途実施してください。