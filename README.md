# AWS AppStream 開発用踏み台

Terraform で Amazon AppStream 2.0 (WorkSpaces Applications) のネットワーク、Fleet、Stack を作成する IaC です。利用者は Linux デスクトップから Chrome や VS Code を使い、開発環境へ接続する想定です。

## 端末の呼び名と用途

GitHub 認証後の選択画面では、次の 2 種類の端末を選べます。呼び名のみの変更で、利用する AWS サービスや接続方法は変わりません。

| 呼び名 | 旧称 / 基盤 | 用途・接続方法 |
| --- | --- | --- |
| セキュアブラウザ | 旧称: AWA Linux / Amazon AppStream 2.0（WorkSpaces Applications）の Linux 環境 | ブラウザーからリモートの Linux デスクトップに接続し、Chrome や VS Code で Web サービス・開発環境を利用します。専用クライアントは不要です。 |
| パーソナルデスクトップ | 旧称: AWP Windows / Amazon WorkSpaces Personal の Windows 環境 | 利用者ごとに割り当てられた Windows デスクトップを利用します。事前に WorkSpaces DCV クライアントをインストールし、初回に設定するディレクトリのパスワードで接続します。 |

### セキュアブラウザ

Web サービスや開発環境へのアクセスに使うリモート環境です。名称はブラウザーを中心とした用途を表しますが、実体は Chrome や VS Code を含む Linux デスクトップであり、ブラウザー専用の別サービスではありません。Fleet の端末をセッションごとに利用し、ホームフォルダ保存・アプリケーション設定の永続化は無効です。必要なファイルは外部のリポジトリなどへ保存してください。時間外などで Fleet が 0 台の場合は、端末の起動準備を待ってから接続します。

### パーソナルデスクトップ

Windows アプリケーションや、利用者専用の継続的な作業環境が必要な場合に使用します。初回にディレクトリユーザーと WorkSpace をオンデマンドで作成し、以降は同じ WorkSpace を再利用します。GitHub ユーザー名は 20 文字以下で、予約名（Admin、Administrator、Guest、krbtgt）以外である必要があり、登録後は変更しないでください。ポータルは GitHub OAuth、端末への接続はディレクトリのパスワードで認証します。構成・制約・導入手順は [WINDOWS-AWP.md](WINDOWS-AWP.md#実装と導入) を参照してください。

## 現在の実装範囲

- 2 つのプライベートサブネットを持つ VPC とリージョナル NAT Gateway
- `stream.standard.xlarge`、1 インスタンスあたり 1 セッションの Linux Always-On Fleet
- AppStream Stack（ホームフォルダ保存と、Linux 非対応のアプリケーション設定永続化は無効）
- 毎日 9:00〜17:00（JST）に待機インスタンスを保ち、時間外は 0 台まで縮小する Fleet のスケーリング
- API Gateway + Lambda による GitHub OAuth 認証、Organization メンバーシップ確認、AppStream 起動
- Secrets Manager による OAuth 資格情報管理、DynamoDB による一回限りの OAuth state 管理
- 共有ポータルからの WorkSpaces Personal オンデマンド登録（[構成と導入](WINDOWS-AWP.md#実装と導入)）
- Codespaces 内の Python 3 / AWS CLI / Terraform と MFA ログインスクリプト

Terraform は公開済みの AppStream イメージを使用します。イメージのビルド・公開処理は含まれていません。AppStream Image Builder で Chrome、VS Code、日本語環境を含むイメージを作成し、そのイメージ名を Codespaces Secret の `APPSTREAM_IMAGE_NAME` に設定してください。Packer の `amazon-ebs` が作成する EC2 AMI は AppStream イメージとして直接利用できません。

具体的な手順は [IMAGE-BUILD.md](IMAGE-BUILD.md) を参照してください。この文書のイメージ作成と Fleet の説明は Linux 版が対象です。共有のログイン画面から利用する Windows WorkSpaces Personal の構成・制約・導入手順は [WINDOWS-AWP.md](WINDOWS-AWP.md#実装と導入) を参照してください。

## 認証・起動の流れ

```text
利用者 → API Gateway /login → Lambda → GitHub OAuth
       → API Gateway /callback → Lambda
       → GitHub API で本人と Organization の active メンバーシップを確認
       → 利用する端末の選択画面
       → セキュアブラウザを選択
       → Fleet の容量を確認（0 台なら 1 台を要求し、起動準備中は待機画面）
       → AppStream CreateStreamingURL → AppStream にリダイレクト
```

パーソナルデスクトップを選択した場合は、初回登録と WorkSpace の作成・起動を経て、WorkSpaces DCV クライアントで接続します。

Cognito、OIDC ラッパー、SAML IdP は使用しません。`github_organization` に指定した Organization の有効なメンバー全員（非公開メンバーを含む）を許可し、招待待ち・非メンバーは拒否します。`read:org` scope のユーザートークンで本人のメンバーシップを確認するため、管理者の PAT は不要です。

OAuth は PKCE（S256）と Secure / HttpOnly / SameSite=Lax Cookie に紐づく state で保護します。state は 10 分間有効で、DynamoDB の条件付き削除により一回だけ使用できます。GitHub のアクセストークンは Lambda の処理中だけ使用し、DB やログには保存しません。AppStream の UserId は変更可能な GitHub ログイン名ではなく `gh-<GitHub の数値 ID>` を使用し、利用者の識別子を安定させます。

認証後の選択・準備状況確認は、同じ DynamoDB テーブルに保存する 1 時間有効のブラウザーセッションを使用します。Cookie の値はハッシュ化して保存し、POST は CSRF トークンと Origin 検証で保護します。メンバーシップは OAuth ログイン時に確認し、セッション中の再確認では GitHub に再認証しません。期限切れの場合はログインし直してください。

AppStream の **End Session** 後は `/session-ended` に遷移し、画面中央に「セッションを終了しました」と、ログイン URL（`/login`）へのリンク「再度ログインする」を表示します。GitHub 自体からはログアウトしません。

## 必要な環境

- Terraform 1.7 以降
- Python 3.10 以降と AWS CLI v2（Dev Container にインストール済み）
- AWS Provider 6.x
- Archive Provider 2.7 以降（2.x、Lambda の ZIP 作成に使用）
- Random Provider 3.7 以降（3.x、ディレクトリ管理者パスワード生成に使用）
- アカウント共通の `workspaces_DefaultRole`（[準備手順](AWS-SETUP.md#workspaces-personal-の共通サービスロール)）
- 対象リージョンに作成済みの AppStream イメージ
- Codespaces の Secrets:

| 名前 | 用途 |
| --- | --- |
| `BUSTION_GITHUB_ORGANIZATION` | 利用を許可する GitHub Organization、リソース名の末尾、`TF_VAR_github_organization` |
| `APPSTREAM_IMAGE_NAME` | 公開済み AppStream イメージ名、`TF_VAR_appstream_image_name` |
| `AWS_ACCESS_KEY_ID` | MFA を登録した IAM ユーザーのアクセスキー ID |
| `AWS_SECRET_ACCESS_KEY` | 同アクセスキーのシークレット |
| `AWS_ROLE_ARN` | Terraform 実行権限を持つ IAM ロール ARN |
| `AWS_SERIAL_NUMBER` | IAM ユーザーに登録した MFA デバイス ARN |

アクセスキー、ロール、MFA の準備は [AWS-SETUP.md](AWS-SETUP.md) を参照してください。GitHub OAuth App の設定は [GITHUB-SETUP.md](GITHUB-SETUP.md) を参照してください。OAuth の Client ID / Secret は apply 後に Secrets Manager に登録します。Terraform 変数や Codespaces Secrets には渡しません。

## 実行

Codespaces の Secrets を設定して Dev Container を作成すると、`post-create.sh` が `~/.aws` に元のアクセスキー用の `base`、MFA 付き AssumeRole の設定用の `terraform-mfa`、取得した一時認証情報用の `terraform-role` プロファイルを準備します。初期化処理は MFA 入力や AWS API 呼び出しを行いません。

Terraform provider は MFA コードを対話入力できないため、先にリポジトリ直下で `python3 scripts/login.py` を実行してください。端末の標準入出力で `MFA OTP (6 digits):` と表示されたら、現在の 6 桁のコードを入力します。スクリプトは AWS CLI で MFA 付き AssumeRole を実行し、アクセスキー・シークレットキー・セッショントークンを `~/.aws/credentials` の `terraform-role` に権限 `600` で保存します。既存の他のプロファイルは保持し、認証情報や OTP を出力せず、有効期限のみ表示します。AWS CLI が返す認証エラー等は標準エラー出力に表示し、失敗時は既存の認証情報を変更しません。

同スクリプトが大文字の `BUSTION_GITHUB_ORGANIZATION` / `APPSTREAM_IMAGE_NAME` を、それぞれ `TF_VAR_github_organization` / `TF_VAR_appstream_image_name` に転記します。設定は `~/.config/aws-bustion/terraform-env.sh` に保存し、`~/.bashrc` から読み込むため、新しく開いた Bash ターミナルで有効です。イメージ名を `-var` で渡す必要はありません。

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

Secret を変更した場合は Codespace を再起動して環境変数を反映し、リポジトリ直下で `bash .devcontainer/post-create.sh` を再実行してください。その後、新しい Bash ターミナルを開くか、既存のターミナルで `source "$HOME/.config/aws-bustion/terraform-env.sh"` を実行します。

一時認証情報は既定で 1 時間有効です。期限切れ時はリポジトリ直下で `python3 scripts/login.py` を再実行してください。`post-create.sh` の再実行は一時認証情報を削除するため、その場合も再ログインが必要です。既存の Dev Container には Python feature を追加するため、コンテナーをリビルドしてください。

apply 後、[GITHUB-SETUP.md](GITHUB-SETUP.md) に従い、出力された callback URL で GitHub OAuth App を作成し、出力された ARN の Secret に資格情報を登録してください。**登録するまではログインできません（503 応答）。** Secret の値は Terraform で作成・取得しないため、Terraform state に入りません。登録後、`appstream_login_url` をブラウザーで開きます。Secret の更新だけなら再 apply は不要です。

既定リージョンは `ap-northeast-1` です。変更する場合は `-var='region=YOUR_AWS_REGION'` のように指定してください。組織名に応じ、AWS リソース名には `bustion-${BUSTION_GITHUB_ORGANIZATION}` が含まれます。

Terraform state は現在ローカルに保存されます。Codespace を削除すると state を失う可能性があるため、継続利用や複数人での運用を始める前に、暗号化・アクセス制御された S3 backend と state ロックを構成してください。

## 注意事項

- Fleet は Always-On です。待機インスタンスは起動済みのため、接続は数十秒程度で始まりますが、未使用時も稼働料金がかかります。Fleet 種別は時刻で切り替えられないため、時間帯ごとの最小容量をスケジュールで変更します。
- 既定では、9:00 に起動済みの1台が待機しているよう、30分前の 8:30 に最小容量を 1（`min_capacity`）へ上げ、17:00 に 0 へ下げます（`Asia/Tokyo`）。最大容量は 22、初期容量は 1 で、同時接続 20 人と待機分を想定しています。時刻は `business_hours_start_schedule` / `business_hours_end_schedule` / `business_hours_timezone` で変更できます。
- 0 台からの起動には数分〜十数分かかるため、開始時刻を 9:00 より早めています。実際の起動時間が30分を超える場合は、開始時刻をさらに早めてください。
- 利用中のセッションがあり、空きが 1 台以下になった時点で 1 台追加します。空き 3 台以上、または利用中 0 で空き 2 台以上の状態が 5 分続くと 1 台削減します。スケールアウトのクールダウンは 120 秒、スケールインは 300 秒です。再 apply では自動調整後の容量を初期値に戻しません。
- 利用中 0 で空きがある状態が 30 分続くと 1 台削減します。日中は最小容量 1 台が残り、17:00 以降は最後の 1 台も停止します。17:00 時点で使用中のセッションは強制終了しません。
- 時間外（0 台）は、GitHub 認証後にセキュアブラウザを選択すると Lambda が `DesiredInstances = 1` を明示的に要求します。CloudWatch の容量不足アラームや AppStream の Retry には依存しません。利用中のセッションも空きもない起動準備中は、起動 URL を発行せず日本語の待機画面（503 / `Retry-After: 20`）を画面中央に表示します。通常 10〜20 分程度かかり、同じブラウザーセッションで 20 秒ごとに `/linux` から容量を確認し、空きができたら AppStream に進みます。手動では「もう一度確認する」を押してください（使用済み callback URL の再読み込みは不可）。要求済みの容量がある間は追加要求しません。既存セッションへの再接続は従来どおりです。
- これらの待ち時間は未使用容量の縮小待ち時間であり、接続中ユーザーの無操作による切断タイムアウトではありません。使用中のセッションはスケールインで強制終了しません。
- リージョナル NAT Gateway、AppStream、共有ポータルで使用する Managed Microsoft AD と WorkSpaces には継続的な料金が発生します。不要時の破棄手順は [WINDOWS-AWP.md](WINDOWS-AWP.md#適用手順) を参照してください。実行時に作成した WorkSpace は `terraform destroy` の管理対象外です。
- AppStream のサービスロール `AmazonAppStreamServiceAccess` は他の AppStream 環境と共用するため Terraform では管理せず、`terraform destroy` でも削除されません。事前に作成しておく必要があります（[AWS-SETUP.md](AWS-SETUP.md) を参照）。
- 認証用の API Gateway、Lambda、DynamoDB、Secrets Manager、CloudWatch Logs にも料金が発生します。Lambda は VPC に接続せず、GitHub と AWS API にアクセスします。
- ホームフォルダ保存（`HOMEFOLDERS`）は無効です。セッション終了後、インスタンス上のファイルや Chrome / VS Code の設定は残りません。AWS provider はストレージコネクターの削除を反映しないため、apply 時に AWS CLI（`terraform-role` プロファイル）で既存 Stack から削除します。既存の S3 バケット内のデータは削除されません。Linux 非対応の Application Settings 永続化も明示的に無効にしています。
- 起動 URL の有効期限は 60 秒です。URL 自体がアクセス権になるため、共有・ログ保存しないでください。この期限はセッションの最大利用時間ではありません。
- Organization の利用権限は OAuth ログイン時に毎回確認します。認証後のブラウザーセッションは最大 1 時間有効です。メンバーを削除しても既存のセッションは即時失効しません。必要な場合は管理者がセッションを終了してください。
- Fleet が停止中・容量不足・起動準備中の場合は接続できません（時間外の自動起動は上記のとおりです）。Fleet の稼働と公開イメージ内のアプリケーションを事前に確認してください。既存の `APP` stream view を維持し、起動 URL に特定の ApplicationId は指定しません。
- API Gateway は公開 HTTPS エンドポイントです。Organization の OAuth App 制限・SSO ポリシーによっては管理者承認が必要です。詳細は [GITHUB-SETUP.md](GITHUB-SETUP.md) を参照してください。
- Secrets Manager の Secret は destroy 時に既定の復旧期間付きで削除予約されます。GitHub OAuth App の削除・失効は別途実施してください。