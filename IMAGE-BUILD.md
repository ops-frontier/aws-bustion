# イメージのビルド手順

Linux と Windows のイメージは別々に作成します。以下の Windows 手順はマルチセッションフリート向けです。**本リポジトリの Terraform は現在 Linux シングルセッション向けであり、Windows イメージ名への差し替えだけではマルチセッション構成にはなりません。** フリート側の注意点は末尾を参照してください。

## Linux イメージ

### Linux イメージとスタックの互換性

Linux ベースのスタックでは [Application Settings 永続化](https://docs.aws.amazon.com/appstream2/latest/developerguide/app-settings-persistence.html)はサポートされません。関連付け時に `IncompatibleImageException` が発生した場合は、イメージを再作成するのではなく、スタックの `application_settings.enabled` を `false` にしてください。本リポジトリの Terraform は明示的に無効化します。

ホームフォルダ保存も無効にしています。セッション中に作成したファイルや Chrome / VS Code のユーザー設定は次回セッションに引き継がれません。全員に共通で必要な設定や拡張機能は、イメージに組み込んでください。

### Image Builder の起動

1. AWSコンソールで Workspaces Application を開く
2. Images で Image Builder タブを開き、Launch Image Builder を開く
3. Image Builder の名称は image-builder-${BUSTION_GITHUB_ORGANIZATION} とし、事前に用意されているVPCに接続して Image Builder を起動する
4. ステータスが Running になるのを待って Connect をクリックして接続する

### イメージの構築
接続後、 Application から　Terminal を起動して、以下のコマンドを実行する。ただし、環境変数 BUSTION_GITHUB_ORGANIZATION は Image Builder 内では設定されていないので、最初に設定しておく必要がある。
```shell
dnf install -y langpacks-ja
sudo dnf install -y langpacks-ja
sudo dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
echo -e '[code]\nname=Visual Studio Code\nbaseurl=https://packages.microsoft.com/yumrepos/vscode\nenabled=1\ngpgcheck=1\ngpgkey=https://packages.microsoft.com/keys/microsoft.asc' | sudo tee /etc/yum.repos.d/vscode.repo
sudo dnf install -y code
xdg-mime default code.desktop x-scheme-handler/vscode
code --install-extension GitHub.codespaces --force
sudo mkdir -p /etc/skel/.vscode/extensions
sudo cp -r ~/.vscode/extensions/* /etc/skel/.vscode/extensions/ || true
AppStreamImageAssistant add-application --name 'GoogleChrome' --display-name 'Google Chrome' --absolute-app-path '/usr/bin/google-chrome-stable' --launch-parameters '
--no-first-run --disable-dev-shm-usage --start-maximized'
AppStreamImageAssistant add-application --name 'VSCode' --display-name 'Visual Studio Code' --absolute-app-path '/usr/bin/code' --launch-parameters '
--new-window'
AppStreamImageAssistant create-image --name "image-bustion-${BUSTION_GITHUB_ORGANIZATION}"
```
最後のコマンドを実行すると自動的に接続が切断されて、image-bustion-${BUSTION_GITHUB_ORGANIZATION} という名前でイメージが生成される。

## Windows イメージ（マルチセッション向け）

### 事前条件と Image Builder の起動

1. WorkSpaces Applications コンソールの **Images > Image Builder > Launch Image Builder** を開き、対象リージョンで提供される最新の AWS Windows Server ベースイメージを選びます。Linux イメージや EC2 AMI は使用しません。[マルチセッション対応の条件](https://docs.aws.amazon.com/appstream2/latest/developerguide/set-up-stacks-fleets-create.html)は、2023 年 5 月 15 日以降のベースイメージまたは Managed Image Update です。使用する Chrome / VS Code が対応する Windows Server バージョンも確認してください。
2. 名前を `image-builder-windows-${BUSTION_GITHUB_ORGANIZATION}` とし、フリートで使用するインスタンスファミリーに対応する Image Builder を選びます。既存 VPC のサブネット、セキュリティグループを指定し、NAT 経由などでソフトウェアの配布元へ HTTPS 接続できるようにします。
3. **Running** になったら接続し、**Administrator** としてログインします。アプリのインストールは管理者で、利用者向け設定と動作確認は後述の非管理者アカウントで行います。
4. **Remote Desktop Session Host と Remote Desktop Licensing はインストールしないでください。** WorkSpaces Applications がフリートインスタンスのプロビジョニング時に構成します（[マルチセッションのイメージ管理](https://docs.aws.amazon.com/appstream2/latest/developerguide/multi-session-recs.html)）。

### Chrome と VS Code のインストール

管理者として、以下を **すべてのユーザー向け** にインストールします。Administrator のプロファイル内だけにインストールすると、フリートの利用者から起動できません。

| アプリケーション | インストール方法 | Image Assistant に登録する実行ファイル |
| --- | --- | --- |
| Google Chrome | [Chrome Enterprise](https://chromeenterprise.google/browser/download/) の Windows 64-bit MSI をダウンロードし、マシン全体にインストール | `C:\Program Files\Google\Chrome\Application\chrome.exe` |
| Visual Studio Code | [公式ダウンロード](https://code.visualstudio.com/download)から Windows x64 の **System Installer** を選択し、管理者権限でインストール（User Installer は使用しない） | `C:\Program Files\Microsoft VS Code\Code.exe` |

- VS Code のセットアップでは `vscode://` URL の処理を登録するオプションを有効にします。Codespaces などのブラウザーから VS Code に戻る認証フローに使用します。System Installer と User Installer の違いは [Windows 向け VS Code の手順](https://code.visualstudio.com/docs/setup/windows)を参照してください。
- 必要な Windows 更新や再起動を済ませ、再接続後に実際のインストール先を確認します。日本語表示や入力が必要な場合は Windows の日本語言語機能も追加し、後述の Template User で表示言語・入力設定を確認します。
- Chrome / VS Code のユーザーデータは各ユーザーのプロファイルに保持します。共通の書き込み可能な `--user-data-dir`、VS Code のポータブル用 `data` フォルダーなどを全セッションで共有しないでください。

### アプリ登録・初期設定・イメージ作成

[AWS の Windows イメージ作成手順](https://docs.aws.amazon.com/appstream2/latest/developerguide/tutorial-image-builder.html)に従い、デスクトップの **Image Assistant** を使用します。

1. **1. Add Apps** で次の 2 アプリを追加します。

   | Name | Display Name | 実行ファイル | Launch parameters |
   | --- | --- | --- | --- |
   | `GoogleChrome` | Google Chrome | 上記の `chrome.exe` | `--no-first-run --start-maximized` |
   | `VSCode` | Visual Studio Code | 上記の `Code.exe` | `--new-window` |

2. **2. Configure Apps > Switch user** で **Template User** に切り替えます。Chrome / VS Code を起動し、全員に配布する初期設定を行います。Linux 版と同じ GitHub Codespaces 拡張機能は、このユーザーの PowerShell で次のようにインストールします。

   ```powershell
   & 'C:\Program Files\Microsoft VS Code\bin\code.cmd' --install-extension GitHub.codespaces --force
   if ($LASTEXITCODE -ne 0) { throw 'GitHub.codespaces installation failed.' }
   $env:USERPROFILE
   ```

   拡張機能は通常ユーザーごとの `%USERPROFILE%\.vscode\extensions` に入ります。Administrator にインストールしただけでは配布されません。個人の GitHub アカウントでのログイン、Settings Sync、パスキー登録はここでは行わず、認証情報をイメージに含めないでください。
3. アプリを閉じ、**Switch user** で Administrator に戻って **Save settings** を実行します。Template User の設定を新規ユーザーの初期設定として保存する操作です。セッション間の Application Settings 永続化とは異なります。拡張機能も確実に新規プロファイルへ配布するため、上記で表示した Template User のプロファイルパスを使い、管理者の PowerShell で次を実行します。

   ```powershell
   $templateProfile = 'C:\Users\<上記で確認した Template User のフォルダー名>'
   New-Item -ItemType Directory -Path 'C:\Users\Default\.vscode' -Force -ErrorAction Stop | Out-Null
   Copy-Item -Path "$templateProfile\.vscode\extensions" -Destination 'C:\Users\Default\.vscode' -Recurse -Force -ErrorAction Stop
   ```

4. **3. Test** で **Test User** に切り替え、管理者権限なしで両アプリを起動できること、初期設定と Codespaces 拡張機能が反映されることを確認します。拡張機能がない場合はそのまま公開せず、新規プロファイルへのコピーと読み取り権限を確認します。`vscode://` リンクで VS Code を開く動作も確認します。
5. Administrator に戻り、**4. Optimize** で両アプリを起動・操作して最適化を完了します。
6. **5. Configure Image** で `image-bustion-windows-${BUSTION_GITHUB_ORGANIZATION}-<version>` など、Linux と区別できる一意の名前を指定し、**6. Review** で確認してイメージを作成します。コンソールでイメージが **Available** になるまで待ちます。更新時も新しい名前で作成し、検証用フリートで確認してから本番に適用します。

### ローカル USB パスキーと WebAuthn 転送の制約

**Windows イメージに Chrome と拡張機能を追加するだけで、WorkSpaces Applications Client から WebAuthn を転送できるとは扱わないでください。** 本手順で確認した WorkSpaces Applications の[対応機能一覧](https://docs.aws.amazon.com/appstream2/latest/developerguide/client-features.html)には USB 転送と条件付きの YubiKey 対応がありますが、WebAuthn API の転送機能・設定手順は確認できません。

- [WorkSpaces の WebAuthn 設定](https://docs.aws.amazon.com/workspaces/latest/userguide/webauthn_windows.html)にある Standard / Enhanced WebAuthn は **Amazon WorkSpaces 向け** です。WorkSpaces Applications（旧 AppStream 2.0）とは別のサービスです。`DCV WebAuthn Redirection` 拡張機能のインストールや WorkSpaces 向けレジストリ設定だけで、本構成でも動作するとは判断しません。WorkSpaces / DCV エージェントを別途イメージに上書きインストールしないでください。
- **WebAuthn 転送**（認証要求をローカル端末で処理）と **USB 転送**（USB デバイス自体をリモート Windows に渡す）は異なります。USB 転送では共有中のデバイスをローカルアプリから使えません。
- YubiKey の FIDO2 パスキー、OTP、PIV / CAC スマートカードは別の認証方式です。「YubiKey 対応」は FIDO2 / WebAuthn の動作保証を意味しません。[スマートカード転送はマルチセッションでは非対応](https://docs.aws.amazon.com/appstream2/latest/developerguide/feature-support-USB-devices-qualified.html)であり、これをパスキー転送の代替設定にはできません。

**WebAuthn 転送が必須なら、本番採用前に AWS Support に WorkSpaces Applications の Windows マルチセッションでの対応可否を確認してください。** 利用するクライアント OS / バージョン、Windows Server ベースイメージ、パスキーの機種・FIDO2 モード、Chrome のバージョン、認証対象サイトを提示します。サポートされる構成と必要なコンポーネントが確認できるまでは、この要件を未検証として扱います。WorkSpaces など別サービスの採用は、このフリートへの設定追加ではなく構成変更になります。

#### USB 転送で代替できるか検証する場合

以下は AWS の[USB デバイスの適格化手順](https://docs.aws.amazon.com/appstream2/latest/developerguide/qualify-usb-devices.html)に基づく検証準備です。**WebAuthn 転送の手順ではなく、FIDO2 パスキーが Windows マルチセッションで動くことを保証するものでもありません。** 対象デバイスと同時接続時の分離を、実フリートで確認する必要があります。

1. **ローカル端末**は Windows を使用し、[WorkSpaces Applications Client](https://clients.amazonappstream.com) の最新版をインストールします。Amazon WorkSpaces 用クライアントとは異なります。インストーラーの **Optional Components > WorkSpaces Applications Client USB Driver** を選択し、USB ドライバーを管理者権限でインストールします（[公式インストール手順](https://docs.aws.amazon.com/appstream2/latest/developerguide/client-application-windows-installation-user.html)）。ブラウザー接続や macOS クライアントでは USB 転送は利用できません。
2. パスキーをローカル端末に挿し、`%LOCALAPPDATA%\AppStreamClient\dcvusblist.exe` を開きます。対象デバイスの **Copy filter string** でフィルター文字列を取得します。実機から取得し、VID / PID などを推測しないでください。
3. Image Builder の **Actions > Create streaming URL > Launch in Client** からネイティブクライアントで接続します。Administrator として `C:\ProgramData\Amazon\Photon\DCV\usb_device_allowlist.txt` に取得したフィルター文字列を追記します。既存の必要なエントリは保持し、全 USB デバイスを許可するワイルドカードは追加しません。このファイルは互換性フィルターであり、セキュリティ境界ではありません。
4. 対象機器に必要なドライバーがある場合のみ、メーカーの手順に従って Image Builder にインストールします。PIV 用ミドルウェアや YubiKey Manager を入れれば FIDO2 が動く、という前提にはしません。
5. Image Builder を再起動し、クライアントで再接続します。**Settings > USB Devices** で対象デバイスの **Share** を有効にし、非管理者の Chrome から対象サイトの認証を試します。成功した場合は、この設定を含むイメージを作成します。
6. そのイメージで作成した **Windows マルチセッションフリート** に異なる 2 ユーザーで同時接続し、それぞれ別の USB パスキーで認証します。各セッションから自分のデバイスだけが使えること、PIN 入力・タッチ・認証成功、切断・再接続後の動作を確認します。Image Builder 単体で成功しただけでは検証完了にしません。
7. 利用者にもセッション開始時の USB 共有操作を案内します。ローカルで使用する際は共有を解除します（[USB デバイスの共有方法](https://docs.aws.amazon.com/appstream2/latest/developerguide/client-application-windows-how-to-share-usb-device-user.html)）。

### マルチセッションフリートと接続経路の設定

- フリートは Windows の **Always-On または On-Demand** とし、**Multiple user sessions** を選び、**Maximum sessions per instance** を設定します。Linux / Elastic フリートではマルチセッションを利用できません。上限値は 2〜50 ですが、開発用途では [AWS の推奨密度](https://docs.aws.amazon.com/appstream2/latest/developerguide/multi-session-recs.html)と Chrome / VS Code の実負荷を基に決めます。
- マルチセッションの容量は **インスタンス数ではなくセッション数** です。API では `MaxSessionsPerInstance` と `ComputeCapacity.DesiredSessions` を使用します。現行 Terraform の `desired_instances`、インスタンス数前提のスケーリング、および起動用 Lambda の容量判定・起動要求は、そのまま流用できません。Windows 化するときはこれらをセッション容量向けに変更し、スケーリングの容量単位・メトリクスも見直す必要があります（[フリートの自動スケーリング](https://docs.aws.amazon.com/appstream2/latest/developerguide/autoscaling.html)）。**この文書の追記では Terraform / Lambda は変更していません。**
- ホームフォルダ保存と Application Settings 永続化は現行 Terraform では無効です。Windows でも有効化しない限り、セッション内の設定変更は次回に引き継がれません。イメージに保存する全員共通の初期設定とは分けて設計してください。
- 本リポジトリの GitHub OAuth 起動処理は、現在ブラウザー向けの `CreateStreamingURL` にリダイレクトします。**Client のインストールだけではネイティブ接続に切り替わりません。** クライアント接続を実装するときは、認証・Organization メンバーシップ確認後の短命な streaming URL を UTF-8 で Base64 化し、`amazonappstream:<Base64 化した streaming URL>` に誘導します（[AWS のクライアント誘導手順](https://docs.aws.amazon.com/appstream2/latest/developerguide/redirect-streaming-session-from-web-to-client.html)）。Base64 は暗号化ではないため、URL をログや共有ドキュメントに残さないでください。起動処理の変更前は、コンソールで作成した検証用 streaming URL を使ってクライアント接続を試します。
- GitHub OAuth によるフリートへの入場と、接続後に Chrome 内で行う WebAuthn 認証は別の段階です。ローカルブラウザーで GitHub にパスキー認証できても、セッション内への転送を検証したことにはなりません。