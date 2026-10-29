# GitHub OAuth App の準備

このリポジトリは API Gateway + Lambda で GitHub OAuth を処理し、指定 Organization の有効なメンバーにだけ Linux / Windows の端末選択画面を表示します。Cognito、OIDC ラッパー、SAML IdP、管理者の PAT は不要です。

## 1. Terraform で callback 先を作成する

[README.md](README.md) の準備を済ませ、Terraform を apply します。GitHub OAuth App が未作成でも AWS リソースは作成できます。

```sh
cd terraform
terraform init
terraform apply
terraform output -raw appstream_login_url
terraform output -raw github_oauth_callback_url
terraform output -raw github_oauth_secret_arn
```

以下の値を控えます。

| Terraform output | 用途 |
| --- | --- |
| `appstream_login_url` | 利用者のログイン URL / OAuth App の Homepage URL |
| `github_oauth_callback_url` | OAuth App の Authorization callback URL |
| `github_oauth_secret_arn` | Client ID / Secret を保存する Secrets Manager の ARN |

callback URL は `https://<API-ID>.execute-api.<region>.amazonaws.com/callback` です。出力値をそのまま使用し、末尾に `/` を付けないでください。Cognito の `/oauth2/idpresponse` ではありません。

この時点では Secret の値が空のため、ログインすると 503 になります。

## 2. OAuth App を作成する

1. GitHub の **Settings → Developer settings → OAuth Apps → New OAuth App** を開きます。Organization 所有にする場合は、その組織の設定から作成します。
2. **Application name** に用途が分かる名前を入力します。
3. **Homepage URL** に `appstream_login_url` の出力値を入力します。
4. **Authorization callback URL** に `github_oauth_callback_url` の出力値を入力します。
5. 作成後、Client ID を控え、**Generate a new client secret** から Client Secret を発行します。

環境ごとに OAuth App を分けてください。API Gateway を作り直して URL が変わった場合は、OAuth App の登録値も更新します。

## 3. Client ID / Secret を Secrets Manager に登録する

AWS コンソールで Terraform の対象リージョンの **Secrets Manager** を開き、`github_oauth_secret_arn` に一致する Secret に次の JSON を値として設定します。

```json
{
  "client_id": "YOUR_GITHUB_OAUTH_CLIENT_ID",
  "client_secret": "YOUR_GITHUB_OAUTH_CLIENT_SECRET"
}
```

Terraform は Secret の入れ物だけを作成し、値を作成・取得しません。**値を `.tf`、`.tfvars`、リポジトリ、Terraform state、Codespaces Secrets に記載しないでください。** `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` 環境変数も使用しません。

AWS CLI を使う場合は、上記 JSON をリポジトリ外の自分だけが読める一時ファイル（権限 `600`）に用意し、`terraform` ディレクトリで実行します。Secret をコマンドラインに直接書くとシェル履歴等に残るため避けてください。

事前にリポジトリ直下で `python3 scripts/login.py` を実行し、MFA コードを入力して `terraform-role` の一時認証情報を取得してください。期限切れ時も同じ手順で再ログインします。

```sh
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw github_oauth_secret_arn)" \
  --secret-string file:///ABSOLUTE/PATH/github-oauth.json \
  --profile terraform-role \
  --region ap-northeast-1
```

パスとリージョンは実環境に合わせ、登録後は一時ファイルを削除してください。登録作業者には、この Secret に対する `secretsmanager:PutSecretValue` が必要です。Lambda には読み取りだけを許可しています。

Lambda はログインごとに最新の Secret を読み取るため、値の登録・更新後の再 apply は不要です。

## 4. Organization のアクセス許可

利用を許可する組織は Terraform の `github_organization` です。Codespaces では `BUSTION_GITHUB_ORGANIZATION` が `TF_VAR_github_organization` に設定されます。

- Lambda は GitHub に `read:org` scope を要求します。コード・リポジトリ読み取りの `repo` scope は要求しません。
- GitHub の `/user` で本人の数値 ID を取得し、`/user/memberships/orgs/<Organization>` の `state` が `active` の場合だけ許可します。
- 非公開の組織メンバーも確認できます。招待待ちの `pending` メンバーや非メンバーは拒否します。
- Organization が OAuth App access restrictions を有効にしている場合、組織管理者による App の承認が必要です。利用者は GitHub の認可画面で対象 Organization へのアクセスを許可してください。
- SAML SSO を使用する Organization では、GitHub 側のポリシーに従って SSO 認証・認可も完了してください。これは AppStream に SAML で接続するという意味ではありません。
- 承認不足、GitHub API 障害、API 制限の場合は起動 URL を発行せずエラーにします。Organization の許可を回避するフォールバックはありません。

## 5. 動作確認

1. `appstream_login_url` をブラウザーで開き、GitHub にログインして認可します。
2. 対象 Organization の有効なメンバーなら端末選択画面が表示され、セキュアブラウザを選ぶと AppStream にリダイレクトされることを確認します。Fleet が稼働し、容量とアプリケーションが利用可能である必要があります。パーソナルデスクトップの確認は [WINDOWS-AWP.md](WINDOWS-AWP.md#実機確認) を参照してください。
3. 別のブラウザープロファイル等を使い、非メンバーでは起動できないことを確認します。
4. 使用済み callback URL の再送や、Cookie のないブラウザーでの callback 呼び出しが 400 になることを確認します。
5. 時間外に利用者がいなくなり Fleet が 0 台になったら、有効なメンバーでログインしてセキュアブラウザを選びます。日本語の待機画面が画面中央に表示され、Fleet の Desired capacity が 1 になることを確認します。準備中に「もう一度確認する」を押しても要求台数が増えず、通常 10〜20 分後に空きができると `/linux` の自動再確認で AppStream に進むことを確認します。AppStream の Retry や使用済み callback の再読み込みは使用しません。
6. 0 台の状態で非メンバーのログインが Fleet を起動しないこと、セッション終了後に時間外の 30 分の無利用で再び 0 台まで縮小することも確認します。これらの確認では実際のインスタンス料金が発生します。
7. AppStream の **End Session** を実行し、`/session-ended` に遷移して「セッションを終了しました」と「再度ログインする」の 2 行が画面中央に表示されることを確認します。「再度ログインする」を押すと `/login` から GitHub 認証を開始します。GitHub 自体からはログアウトしません。

エラー時は Lambda の CloudWatch Logs（`/aws/lambda/github-auth-bustion-<Organization>`）を確認してください。エラーコードを記録し、GitHub のコード・トークン・Client Secret・AppStream 起動 URL・Windows のパスワードは記録しません。API Gateway のリクエスト URL 全体、POST 本文、Lambda イベント全体を追加でログに出さないでください。

| 応答 | 主な原因 |
| --- | --- |
| 400 | state の不一致・10 分の期限切れ・再使用、認可キャンセル、コード交換失敗 |
| 403 | 非メンバー、招待待ち、ブラウザーセッション期限切れ、CSRF / Origin 不一致、Windows 登録後のユーザー名変更 |
| 409 | Windows ユーザー名の衝突、所有者を確認できない AD ユーザー |
| 502 | GitHub API の障害・アクセス制限・不正な応答 |
| 503 | Linux 起動準備中（待機画面・60 秒ごとに再確認）、Secret 未登録・形式不備、AWS 権限不足、Fleet / 起動処理が利用不能 |

## 6. トークン管理と失効

GitHub のアクセストークンは本人・メンバーシップ確認にだけ使用し、Lambda の処理後に永続保存しません。リフレッシュトークンによる継続利用もしません。ただし保存しないことと GitHub 側で失効することは別です。利用を取り消す場合は、GitHub の **Settings → Applications → Authorized OAuth Apps** から認可を取り消してください。

Client Secret が漏えいした場合は GitHub 側で再発行し、Secrets Manager の値を更新した後、古い Secret を失効させます。OAuth App を削除しても開始済みのセッションは終了しません。Organization からの削除は次回 OAuth ログインから反映されます。認証後のブラウザーセッションは最大 1 時間有効で、WorkSpaces クライアントの新規接続は AD パスワードと RADIUS MFA の OTP で認証します。既存の接続は即時終了しません。即時遮断が必要ならポータルのセッション失効と、AppStream セッション終了または AD ユーザーの無効化なども管理者が実施してください。
