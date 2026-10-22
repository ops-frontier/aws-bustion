# イメージのビルド手順

WorkSpaces Applications の Linux シングルセッションフリートで使用するイメージを作成します。

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
