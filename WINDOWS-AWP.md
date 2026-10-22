Windowsの仮想端末を AWP(Amazon Workspaces Personal)で構築することにしました。以下に説明しますので、記述に従って実装変更をお願いします。

# 既存との関係

- VPC と API Gateway, Lambda は AWA(Amazon Workspaces Application) Linux版と共有し、Login のURL も同じものを使用してください
- AWS Windows 版は導入を断念しましたので、REAME.md, IMAGE-BUILD.md から記述を完全に削除し、 Linux版のみの状態で記述に矛盾がないように修正してください

# 利用手順
現行の API Gateway + Lambdaを修正して、以下の手順で利用できるようにする。原則として Web に表示するメッセージ類は日本語化し、画面中央に表示する。

1. Login URL からログインする
2. API Gateway で Github OAuth でログインする（組織の所属チェックは現行のまま行う）
3. ログインに成功すると AWA Linux を使用するか AWP Windows を使用するかの選択画面が表示される（この画面でAWP DCV Client のインストール画面 https://clients.amazonworkspaces.com/ へのリンクを表示し、AWPを使用する場合は先にAWP DCV Client をインストールするように促す）
4. AWA Linux を選択した場合は、現行の AWA 起動処理を呼び出す
5. AWP Windows を選択した場合は、続く処理を実施する
6. ディレクトリに Github OAuth のIDと同名のユーザが存在しない場合は、ディレクトリにユーザを追加する。その際はパスワード入力欄と確認欄を持ったフォームを表示して、ユーザにパスワードを入力させ、ディレクトリのユーザのパスワードとする。ここで、ユーザに対して GithubのIDは20文字以下でAdmin、Administrator、Guest、krbtgt以外でなければならないという制約を課し、変更を禁止する。時間がかかる場合は登録を待つ画面を表示し、登録されるのを待つ。
7. 対応する Workspaces が存在しない場合は Workspaces を生成する。時間がかかる場合は登録を待つ画面を表示し、登録されるのを待つ。
8. Workspaces のクライアントを起動するURL workspaces://ユーザID@登録コード を表示してユーザにクリックさせる。起動に失敗した時のためにAWP DCV Client のインストール画面 https://clients.amazonworkspaces.com/ へのリンクを表示し、起動できない場合はインストールするように促す。
9. ユーザはクライアントの起動を許可して起動し、パスワードを入力してログインする

# ディレクトリ
以下のパラメータで Terraform で作成する。API Gateway + Lambda からユーザをオンデマンドに追加する。

エディション: Standard Edition
名称：${BUSTION_GITHUB_ORGANIZATION}.bustion.ops-frontier.dev
ディレクトリのタイプ: Microsoft AD
組織名: ${BUSTION_GITHUB_ORGANIZATION}
VPCとサブネット: AWA Linux　と共有
Admin パスワード: 乱数生成

# Workspace
API Gateway + Lambda から以下のパラメータでオンデマンドにワークスペースを追加する。ユーザは1個しかワークスペースを作れない。

ユーザネーム: Github OAuth でログインしたID（ユーザに対して GithubのIDは20文字以下でAdmin、Administrator、Guest、krbtgt以外でなければならないという制約を課し、変更を禁止する。）
プロトコル: DCV
自動停止時間:1時間
バンドル： 
  名前: Standard with Windows (Server 2025 based)
  ハードウェア: 2 vCPU、4.0 GB メモリ、50 GB ストレージ
  ID: wsb-96cqc2ksn
  言語: Japanese

# 実装と導入

## 作成するリソース

- 既存の VPC、2 つのプライベートサブネット、NAT、API Gateway、Python Lambda、ログイン URL を共有する。
- Standard Edition の AWS Managed Microsoft AD を `${BUSTION_GITHUB_ORGANIZATION}.bustion.ops-frontier.dev` で作成し、Directory Service Data を有効にする。NetBIOS 名は組織名の先頭 15 文字を大文字にする。
- Admin パスワードは Random Provider で生成する。**パスワードは Terraform state / バックアップに含まれるため、アクセス制限と安全な保管が必要。** Lambda には Admin パスワードを渡さない。
- WorkSpaces にディレクトリを登録する。事前にアカウント共通の `workspaces_DefaultRole` が必要（[AWS-SETUP.md](AWS-SETUP.md#workspaces-personal-の共通サービスロール)）。
- 利用者ごとの WorkSpace は Terraform ではなく Lambda が作成する。指定バンドル `wsb-96cqc2ksn` の既定のハードウェア・ストレージを使用し、`AUTO_STOP` / 60 分 / DCV に設定する。AWS API / SDK 上のプロトコル値は旧名称の `WSP`。バンドルの利用可否と実際の構成は対象リージョンで確認する。
- WorkSpace の作成・登録には継続的な費用とサービスクォータが適用される。AD は WorkSpace の停止中も課金される。

## 利用者の識別とパスワード

認証後に AWA Linux / AWP Windows の選択画面を表示する。Windows の初回登録時にパスワードを 2 回入力し、Directory Service Data `CreateUser` と Directory Service `ResetUserPassword` を呼ぶ。パスワードは 8〜127 文字、3 種類以上の文字種とディレクトリのポリシーが必要で、保存・ログ出力しない。AD 反映直後のパスワード設定失敗時は、少し待ってフォームから再入力する。

GitHub ログイン名を小文字の `SAMAccountName` に使用し、20 文字超または予約名は Windows のみ拒否する。GitHub の名前変更そのものは本ツールから禁止できないため、登録後の名前変更を検出した時点で Windows の利用を拒否する。

数値 GitHub ID と初回ログイン名は、TTL のない `windows-users-bustion-<Organization>` テーブルでトランザクションにより一対一に固定する。変更後の名前や、他人が再利用した名前を既存 WorkSpace に結び付けない。AD の description にも所有者マーカーを保存し、所有者を確認できない既存 AD ユーザーは引き継がない。手動作成ユーザーの移行は管理者による所有者確認が必要。

登録完了後はパスワードを再設定しない。AD ユーザーが削除・無効化されていた場合、自動で再作成・有効化しない。

## 待機と接続

ブラウザーセッションは認証後 1 時間有効。作成・起動待ちは 30 秒ごとの `/windows` 再読み込みで進む。Lambda 自体で長時間待機せず、パスワードを非同期処理や DB に保存しない。セッション切れの場合は同じログイン URL から認証し直して Windows を選び、続行する。

ユーザー単位の排他リースと永続的な作成要求記録で、二重クリック・並行リクエストによる重複作成を防ぐ。既存の WorkSpace があれば再利用する。`AVAILABLE` または `STOPPED` になると `workspaces://<ユーザー名>@<登録コード>` を表示する。`STOPPED` の端末はクライアント接続時に起動する。選択画面・接続画面の両方にクライアントのインストールリンクを表示する。

このポータルは GitHub OAuth で認証するが、WorkSpaces クライアントは AD のパスワードで認証する。組織所属は OAuth ログイン時に確認し、既存ブラウザーセッションやクライアント接続を即時失効させる機能はない。退職・脱退時は管理者が AD アカウント無効化などを実施する。ユーザーデータの自動削除は行わない。

指定の公開バンドルをそのまま使うため、Chrome / VS Code の組み込みや WebAuthn 有効化はこのプロビジョニング処理には含まれない。動作確認済みの WebAuthn 設定を新規端末にも適用する。Linux の AppStream イメージを流用することはできない。

## 適用手順

共通サービスロールと Terraform 実行権限を準備し、リポジトリ直下で MFA ログイン後に実行する。

```sh
python3 scripts/login.py
cd terraform
terraform init
terraform plan
terraform apply
terraform output -raw appstream_login_url
terraform output -raw workspaces_directory_id
terraform output -raw workspaces_registration_code
```

OAuth の Secret と callback URL は既存と同じ。新しい OAuth App は不要。WorkSpaces は実行時に作成されるため、**`terraform destroy` だけでは削除されない**。破棄する場合は利用者データを退避し、管理者が全 WorkSpace を削除してからディレクトリ登録解除・AD 削除を行う。DynamoDB の本人対応表を WorkSpace より先に削除しない。

## 障害時の復旧

CloudWatch Logs でエラーコードを確認する。イベント全体、POST 本文、パスワード、Cookie、OAuth トークンは記録しない。

- AD のユーザー作成後にパスワード設定だけ失敗した場合は、フォームから再入力する。同じ所有者マーカーのユーザーで続行し、二重作成しない。
- WorkSpaces への AD 反映遅延が明示的に返された場合のみ自動再試行する。
- 作成 API の通信結果が不明な場合は追加要求せず既存 WorkSpace の出現を待つ。30 分間見つからない場合はエラーを表示する。明示的な作成拒否はエラーとして保存し、再要求しない。
- 管理者は `DescribeWorkspaces`、CloudTrail、サービスクォータ、IAM、バンドルを確認する。**作成中・既存の端末がないことを確認してから**、対応する `github:<数値 ID>` レコードの `workspace_requested` と `workspace_failure` のみを削除すると再試行できる。本人対応や `login:<名前>` レコードは削除しない。
- Lambda タイムアウト後の排他リースは最大 60 秒で失効する。リース待ち中は待機画面を表示する。

## 実機確認

1. 有効な組織メンバーでログインし、選択画面とクライアントのインストールリンクを確認する。
2. AWA Linux の既存起動・0 台からの起動待ちが維持されていることを確認する。
3. Windows を選び、初回だけパスワードフォームが出ること、不一致やポリシー違反が拒否されることを確認する。
4. AD に同名のユーザーが 1 人、指定バンドル・DCV・AutoStop 60 分の WorkSpace が 1 台作成されることを確認する。
5. 作成待ち中の再読み込みや別タブでの操作で重複作成されず、接続可能になるとクライアント起動リンクが出ることを確認する。
6. クライアントでパスワード認証し、設定済みの WebAuthn 転送で USB パスキーが使えることを確認する。
7. 再ログインでパスワード設定・WorkSpace 作成を繰り返さず、停止済み端末にも接続できることを確認する。
8. 非メンバー、セッション期限切れ、禁止名・長すぎる名前、GitHub の名前変更・再利用を拒否することを確認する。

実機確認では WorkSpaces / AD の料金が発生する。ローカルの Lambda テストと Terraform モックテストは AWS リソースを作成しない。
