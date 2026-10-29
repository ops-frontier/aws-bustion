Windowsの仮想端末を AWP(Amazon Workspaces Personal)で構築することにしました。以下に説明しますので、記述に従って実装変更をお願いします。

# 既存との関係

- VPC と API Gateway, Lambda は AWA(Amazon Workspaces Application) Linux版と共有し、Login のURL も同じものを使用してください
- AWS Windows 版は導入を断念しましたので、REAME.md, IMAGE-BUILD.md から記述を完全に削除し、 Linux版のみの状態で記述に矛盾がないように修正してください

# 利用手順
現行の API Gateway + Lambdaを修正して、以下の手順で利用できるようにする。原則として Web に表示するメッセージ類は日本語化し、画面中央に表示する。

1. Login URL からログインする
2. API Gateway で Github OAuth でログインする（組織の所属チェックは現行のまま行う）
3. ログインに成功するとセキュアブラウザかパーソナルデスクトップの選択画面が表示される（この画面で WorkSpaces DCV クライアントのインストール画面 https://clients.amazonworkspaces.com/ へのリンクを表示し、パーソナルデスクトップを使用する場合は先にインストールするように促す）
4. セキュアブラウザを選択した場合は、現行の AWA 起動処理を呼び出す
5. パーソナルデスクトップを選択した場合は、続く処理を実施する
6. 初回はパスワード入力欄・確認欄と、認証アプリ用の OTP 登録 QR コードを表示する。6 桁の確認コードが正しい場合だけ OTP を有効化して、ディレクトリに Github OAuth のIDと同名のユーザを追加し、入力パスワードを設定する。Github のIDは20文字以下でAdmin、Administrator、Guest、krbtgt以外でなければならず、登録後は変更しない。既存の AD ユーザーはパスワードを変更せず OTP のみ登録する。時間がかかる場合は登録を待つ画面を表示する。
7. 対応する Workspaces が存在しない場合は Workspaces を生成する。時間がかかる場合は登録を待つ画面を表示し、登録されるのを待つ。
8. Workspaces のクライアントを起動するURL workspaces://ユーザID@登録コード を表示してユーザにクリックさせる。起動に失敗した時のためにAWP DCV Client のインストール画面 https://clients.amazonworkspaces.com/ へのリンクを表示し、起動できない場合はインストールするように促す。
9. ユーザはクライアントの起動を許可して起動し、AD パスワードと認証アプリの現在の OTP を入力してログインする。登録確認に使用したコードは再利用できないため、次のコードへの切り替わりを待つ。

# ディレクトリ
以下のパラメータで Terraform で作成する。API Gateway + Lambda からユーザをオンデマンドに追加する。

エディション: Standard Edition
名称：${BUSTION_GITHUB_ORGANIZATION}.bustion.ops-frontier.dev
ディレクトリのタイプ: Microsoft AD
組織名: ${BUSTION_GITHUB_ORGANIZATION}
VPCとサブネット: セキュアブラウザ（AppStream Linux）と共有
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
- RADIUS 用のプライベート `t4g.micro`（1 GiB RAM）EC2 を 1 台常駐させる。Ubuntu 24.04 ARM64、暗号化した 8 GiB gp3 EBS、IMDSv2 を使用し、SSH とパブリック IP は無効。パッケージ取得と AWS API 接続には既存 NAT を使用する。
- Managed Microsoft AD の RADIUS 設定を PAP / UDP 1812 で有効化する。AD のパスワードは AD が、OTP は FreeRADIUS が検証する。SAML IdP と CBA は使用しない。
- 専用 DynamoDB に TOTP の暗号化シードと登録・試行状態を保存し、KMS キーで暗号化する。OTP は公開鍵方式ではなく共有秘密方式のため、シードと QR コードの保護が必要。

## 利用者の識別とパスワード

認証後にセキュアブラウザ / パーソナルデスクトップの選択画面を表示する。パーソナルデスクトップの初回登録時にパスワードを 2 回入力し、認証アプリに QR コードを登録して 6 桁の確認コードを入力する。OTP 確認後に Directory Service Data `CreateUser` と Directory Service `ResetUserPassword` を呼ぶ。パスワードは 8〜127 文字、3 種類以上の文字種とディレクトリのポリシーが必要で、保存・ログ出力しない。AD 反映直後のパスワード設定失敗時は、少し待ってフォームから再入力する。OTP の登録完了後は QR を再表示せず、同じ認証アプリを引き続き使用する。

GitHub ログイン名を小文字の `SAMAccountName` に使用し、20 文字超または予約名は Windows のみ拒否する。GitHub の名前変更そのものは本ツールから禁止できないため、登録後の名前変更を検出した時点で Windows の利用を拒否する。

数値 GitHub ID と初回ログイン名は、TTL のない `windows-users-bustion-<Organization>` テーブルでトランザクションにより一対一に固定する。変更後の名前や、他人が再利用した名前を既存 WorkSpace に結び付けない。AD の description にも所有者マーカーを保存し、所有者を確認できない既存 AD ユーザーは引き継がない。手動作成ユーザーの移行は管理者による所有者確認が必要。

登録完了後はパスワードを再設定しない。AD ユーザーが削除・無効化されていた場合、自動で再作成・有効化しない。

## 待機と接続

ブラウザーセッションは認証後 1 時間有効。作成・起動待ちは 20 秒ごとの `/windows` 再読み込みで進む。Lambda 自体で長時間待機せず、パスワードを非同期処理や DB に保存しない。セッション切れの場合は同じログイン URL から認証し直してパーソナルデスクトップを選び、続行する。

ユーザー単位の排他リースと永続的な作成要求記録で、二重クリック・並行リクエストによる重複作成を防ぐ。既存の WorkSpace があれば再利用する。`AVAILABLE` または `STOPPED` になると `workspaces://<ユーザー名>@<登録コード>` を表示する。`STOPPED` の端末はクライアント接続時に起動する。選択画面・接続画面の両方にクライアントのインストールリンクを表示する。

このポータルは GitHub OAuth で認証するが、WorkSpaces クライアントは AD パスワードと RADIUS MFA の OTP で認証する。組織所属は OAuth ログイン時に確認し、既存ブラウザーセッションやクライアント接続を即時失効させる機能はない。退職・脱退時は管理者が AD アカウント無効化などを実施する。ユーザーデータの自動削除は行わない。

指定の公開バンドルをそのまま使うため、Chrome / VS Code の組み込みや WebAuthn 有効化はこのプロビジョニング処理には含まれない。動作確認済みの WebAuthn 設定を新規端末にも適用する。Linux の AppStream イメージを流用することはできない。

## 適用手順

共通サービスロールと Terraform 実行権限を準備し、リポジトリ直下で MFA ログイン後に実行する。

```sh
python3 scripts/login.py
cd terraform
terraform init
bash ../scripts/build_qr_layer.sh
terraform plan
terraform apply
terraform output -raw appstream_login_url
terraform output -raw workspaces_directory_id
terraform output -raw workspaces_registration_code
```

OAuth の Secret と callback URL は既存と同じ。新しい OAuth App は不要。WorkSpaces は実行時に作成されるため、**`terraform destroy` だけでは削除されない**。破棄する場合は利用者データを退避し、管理者が全 WorkSpace を削除してからディレクトリ登録解除・AD 削除を行う。DynamoDB の本人対応表を WorkSpace より先に削除しない。

## RADIUS MFA の導入と運用

**MFA はディレクトリ全体に適用される。apply 後は既存ユーザーも OTP 未登録のまま新規接続できない。** メンテナンス時間を確保し、ポータルからの登録手順を先に案内する。既存ユーザーはパーソナルデスクトップを選択し、QR と確認コードで追加登録する。パスワードは再設定されない。登録済み OTP の表示・変更機能や、パスワードのみへのフォールバックは用意しない。既存の接続を即時切断する設定ではない。

- TOTP は SHA-1、6 桁、30 秒、有効な時刻範囲は前後 1 ステップ。認証アプリで QR を読み取るか、表示されたキーを手動登録する。QR は Lambda 内で SVG として生成し、外部サービスには送信しない。未確認の登録は 10 分で期限切れとなる。
- ユーザー単位で 5 分の固定ウィンドウに 5 回まで確認を許可する。登録確認と DCV 接続で同じ制限・使用済みステップを共有し、条件付き更新でコードの並行再利用も拒否する。上限時は最大 5 分待ち、新しいコードで再試行する。意図的な失敗で一時的に接続を妨害される可能性はある。
- パスワード漏洩だけでは接続できなくなるが、TOTP はリアルタイムのフィッシング、認証アプリごとの侵害、セッション窃取を排除しない。AD のロックアウトポリシー、端末保護、監視も維持する。Windows のロック解除など、WorkSpaces 接続以外のすべての AD ログオンに MFA を追加するものではない。
- RADIUS/UDP は TLS ではないため VPC 内だけで使用する。EC2 のセキュリティグループは AD のセキュリティグループからの UDP 1812 のみ受信し、FreeRADIUS は AD コントローラーの IP と共有秘密を検証する。Message-Authenticator を必須とし、署名のない要求は拒否する。実際の Directory Service の要求との互換性を実機で確認し、問題があっても検証を無効化せず AWS Support に確認する。
- `mfa-bustion-<Organization>` のシードは KMS でログイン名に結び付けて暗号化する。RADIUS 共有秘密は Secrets Manager から EC2 起動設定時に取得する。共有秘密と AD 管理者パスワードは Terraform state に含まれるため、state とバックアップを厳重に管理する。
- サーバー時刻は Amazon Time Sync を使用する。利用者の認証アプリ側も自動時刻設定を有効にする。

EC2 は SSM で管理する。`terraform output -raw radius_instance_id` で対象を確認し、Session Manager 等で次を確認する（ホスト上で実行）。

```sh
sudo cloud-init status --wait
sudo systemctl is-active freeradius
sudo freeradius -C
chronyc tracking
sudo journalctl -u freeradius --since '30 minutes ago'
sudo tail -n 100 /var/log/freeradius/radius.log
```

FreeRADIUS のデバッグモード（`-X` / `-x`）は入力属性や共有秘密を表示しうるため、本番の認証情報を使って実行しない。通常ログにも OTP・シード・パスワードを追加しない。

EC2 は 1 台のため、停止・パッチ適用・置換・依存 AWS API の障害中は新規接続が失敗する。cloud-init 完了前も接続できない。EC2 ステータスアラームを運用監視に接続し、RADIUS プロセスと認証結果も監視する（アラーム通知先は未設定）。コードや起動設定の変更は EC2 を置換する。共有秘密を変更するときも Terraform で EC2 設定と AD の両方を更新する。

Terraform は SSM Run Command の実行結果で cloud-init 完了、FreeRADIUS の稼働と設定検証成功を最大 20 分待ってから、ディレクトリの MFA を有効化する。Terraform 実行環境の AWS CLI と `terraform-role` プロファイルを使用する。EC2 が running でも初期化中は RADIUS に応答できない。以前の構成で RADIUS create が Failed になった場合は、起動失敗の有無と SSM の接続状態を確認し、この待機処理を含む構成で再度 plan / apply する。Terraform 実行ロールには SSM の `SendCommand`、`DescribeInstanceInformation`、`ListCommandInvocations`、`GetCommandInvocation`、`CancelCommand` 権限も必要。PAP 専用サーバーでは標準 EAP モジュールを無効化し、未定義の Auth-Type EAP による起動失敗を防ぐ。準備完了後も失敗する場合は、RADIUS ログで送信元・Message-Authenticator・共有秘密の問題を確認し、OTP 検証を迂回する許可ルールは追加しない。

認証アプリ紛失時の自動リセットは提供しない。管理者が別経路で本人と数値 GitHub ID の対応を確認し、必要なら AD ユーザーを一時無効化して既存のポータルセッションを失効させる。その後、当該ログイン名の MFA レコードだけを削除する。AD を無効化した場合は管理者立ち会いで再有効化してから、新しい GitHub OAuth ログインで再登録させる（AD 無効化中はポータルも登録を拒否する）。MFA 未登録の間は RADIUS が接続を拒否する。本人対応テーブルを削除したり、ディレクトリ全体の MFA を無効にして復旧しない。

## 障害時の復旧

Lambda の CloudWatch Logs とホスト上の RADIUS ログでエラーコードを確認する。イベント全体、POST 本文、パスワード、Cookie、OAuth トークン、OTP、シード、QR コードは記録しない。

- AD のユーザー作成後にパスワード設定だけ失敗した場合は、フォームから再入力する。同じ所有者マーカーのユーザーで続行し、二重作成しない。
- WorkSpaces への AD 反映遅延が明示的に返された場合のみ自動再試行する。
- 作成 API の通信結果が不明な場合は追加要求せず既存 WorkSpace の出現を待つ。30 分間見つからない場合はエラーを表示する。明示的な作成拒否はエラーとして保存し、再要求しない。
- 管理者は `DescribeWorkspaces`、CloudTrail、サービスクォータ、IAM、バンドルを確認する。**作成中・既存の端末がないことを確認してから**、対応する `github:<数値 ID>` レコードの `workspace_requested` と `workspace_failure` のみを削除すると再試行できる。本人対応や `login:<名前>` レコードは削除しない。
- Lambda タイムアウト後の排他リースは最大 60 秒で失効する。リース待ち中は待機画面を表示する。

## 実機確認

1. 有効な組織メンバーでログインし、選択画面とクライアントのインストールリンクを確認する。
2. セキュアブラウザの既存起動・0 台からの起動待ちが維持されていることを確認する。
3. パーソナルデスクトップを選び、初回だけパスワードと QR / OTP 確認フォームが出ることを確認する。実際の認証アプリで QR を読み取り、コード未入力・誤り・パスワード不一致では AD / WorkSpace が作成されないことを確認する。
4. AD に同名のユーザーが 1 人、指定バンドル・DCV・AutoStop 60 分の WorkSpace が 1 台作成されることを確認する。
5. 作成待ち中の再読み込みや別タブでの操作で重複作成されず、接続可能になるとクライアント起動リンクが出ることを確認する。
6. Managed Microsoft AD の RADIUS Status が Enabled、cloud-init が完了、FreeRADIUS が稼働していることを確認する。DCV クライアントにユーザー名・AD パスワード・現在の OTP を入力し接続する。誤ったパスワード、誤った OTP、OTP の省略、同一コードの再利用では接続できないことを確認する。確認直後は次の OTP を待つ。
7. 再ログインでパスワード設定・WorkSpace 作成を繰り返さず、停止済み端末にも接続できることを確認する。
8. 非メンバー、セッション期限切れ、禁止名・長すぎる名前、GitHub の名前変更・再利用を拒否することを確認する。
9. 既存ユーザーがパスワードの再設定なしに OTP を追加登録でき、登録後は QR が再表示されないことを確認する。
10. 同じユーザーの誤った OTP を 5 回送った後は正しい OTP も拒否され、5 分後に新しい OTP で再開できることを確認する。RADIUS を一時停止するとパスワードだけで新規接続できないことも確認し、直ちに再開する。既存セッションを必ず終了させてから新規ログインで試す。

実機確認では WorkSpaces / AD の料金が発生する。ローカルの Lambda テストと Terraform モックテストは AWS リソースを作成しない。
