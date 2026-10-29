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

以下は Rocky Linux の GNOME / X11 セッション向けの手順です。Image Builder に接続後、Applications から Terminal を起動し、順番に実行します。`BUSTION_GITHUB_ORGANIZATION` は Image Builder 内では設定されないため、実際の Organization 名を設定してください。

#### 1. アプリケーションと日本語 IME のインストール

`langpacks-ja` だけでは漢字変換の設定は完了しません。[AWS 公式の日本語対応手順](https://docs.aws.amazon.com/ja_jp/appstream2/latest/developerguide/enable-japanese-support-linux.html)に沿って、IBus とかな漢字変換エンジン KKC をインストールします。

フォントは Rocky Linux 8 の AppStream リポジトリで提供される `google-noto-sans-cjk-ttc-fonts`（Noto Sans CJK）を使用します。AWS 公式例の `vlgothic-p-fonts` は、この環境の有効なリポジトリでは見つからないため使用しません。

```shell
export BUSTION_GITHUB_ORGANIZATION='実際のOrganization名'
sudo dnf install -y langpacks-ja glibc-langpack-ja google-noto-sans-cjk-ttc-fonts ibus ibus-kkc
sudo dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
echo -e '[code]\nname=Visual Studio Code\nbaseurl=https://packages.microsoft.com/yumrepos/vscode\nenabled=1\ngpgcheck=1\ngpgkey=https://packages.microsoft.com/keys/microsoft.asc' | sudo tee /etc/yum.repos.d/vscode.repo
sudo dnf install -y code
xdg-mime default code.desktop x-scheme-handler/vscode
code --install-extension GitHub.codespaces --force
sudo mkdir -p /etc/skel/.vscode/extensions
sudo cp -r ~/.vscode/extensions/. /etc/skel/.vscode/extensions/
```

パッケージが見つからない場合は、`cat /etc/os-release` と `dnf repolist` で Rocky Linux のバージョンとリポジトリを確認してください。`dnf list --available google-noto-sans-cjk-ttc-fonts` で候補も確認できます（導入済みの場合は `rpm -q google-noto-sans-cjk-ttc-fonts`）。IME を導入できないまま後続手順へ進めないでください。

導入後は `rpm -q google-noto-sans-cjk-ttc-fonts ibus ibus-kkc` と `fc-list :lang=ja family` でパッケージと日本語フォントを確認します。フォントの導入は文字表示のためであり、漢字変換には次節の IME 初期化も必要です。

この手順では表示言語やタイムゾーンは変更しません。デスクトップ全体も日本語化する場合は、AWS 公式手順のロケール・cloud-init・タイムゾーン設定を追加してください。日本語キーボードの配列設定と IME による漢字変換は別の設定です。

#### 2. 利用者セッションで日本語入力を初期化する

Image Builder の管理者で入力できても、配信先の利用者で入力できるとは限りません。AWS 公式手順と同様に、利用者自身の GNOME セッションから D-Bus などの環境を引き継ぎ、IME を有効にしてからアプリケーションを起動します。Chrome と VS Code の両方で同じラッパーを使います。

```shell
sudo tee /usr/local/bin/with-japanese-input.sh > /dev/null <<'EOF'
#!/bin/bash
set -euo pipefail

log_dir="${XDG_CACHE_HOME:-$HOME/.cache}/secure-browser"
mkdir -p "$log_dir"
exec > >(tee -a "$log_dir/ime.log") 2>&1
trap 'printf "Japanese input initialization failed at line %s\n" "$LINENO" >&2' ERR

if [ "$#" -eq 0 ]; then
    printf 'Usage: with-japanese-input.sh command [arguments...]\n' >&2
    exit 1
fi

gnome_pid=$(pgrep -u "$(id -u)" -o -x gnome-shell)
if [ ! -r "/proc/$gnome_pid/environ" ]; then
    printf 'Cannot read the current user GNOME session environment\n' >&2
    exit 1
fi

while IFS= read -r -d '' env_var; do
    case "$env_var" in
        DBUS_SESSION_BUS_ADDRESS=*|DISPLAY=*|XAUTHORITY=*)
            export "$env_var"
            ;;
    esac
done < "/proc/$gnome_pid/environ"

: "${DBUS_SESSION_BUS_ADDRESS:?GNOME session D-Bus address is required}"
export GTK_IM_MODULE=ibus
export QT_IM_MODULE=ibus
export XMODIFIERS=@im=ibus

printf 'Initializing Japanese input for user %s\n' "$(id -un)"
ibus version
gsettings set org.freedesktop.ibus.general use-system-keyboard-layout true
gsettings set org.gnome.desktop.input-sources sources "[('xkb','us'), ('ibus','kkc')]"
gsettings set org.gnome.desktop.wm.keybindings switch-input-source "['<Control>space']"
gsettings set org.gnome.desktop.wm.keybindings switch-input-source-backward "['<Shift><Control>space']"

if ! pgrep -u "$(id -u)" -x ibus-daemon > /dev/null; then
    ibus-daemon --daemonize --xim
fi

for attempt in {1..10}; do
    set_status=0
    if ibus engine xkb:us::eng; then
        set_status=0
    else
        set_status=$?
    fi
    current_engine=''
    get_status=0
    if current_engine=$(ibus engine); then
        get_status=0
    else
        get_status=$?
    fi
    printf 'IME attempt %s: set_status=%s get_status=%s engine=%s\n' \
        "$attempt" "$set_status" "$get_status" "$current_engine"
    if [ "$set_status" -eq 0 ] && [ "$get_status" -eq 0 ] && [ "$current_engine" = 'xkb:us::eng' ]; then
        exec "$@"
    fi
    sleep 1
done

printf 'Could not select and verify English input; check %s/ime.log\n' "$log_dir" >&2
exit 1
EOF
sudo chown root:root /usr/local/bin/with-japanese-input.sh
sudo chmod 0755 /usr/local/bin/with-japanese-input.sh
```

GitHub へのログインなど英字入力から始めるため、起動時は英語（US）入力を選択します。日本語 KKC は入力ソースに登録したままで、利用者が必要に応じて切り替えます。GNOME の入力ソース表示と実際の IBus エンジンがずれないよう、IBus のエンジンも明示的に英語へ揃えます。起動待ちは最大 10 回とし、`ibus engine xkb:us::eng` の成功と `ibus engine` の取得結果が `xkb:us::eng` であることを確認してからアプリケーションを起動します。失敗時はアプリケーションを起動せずエラーを出します。標準出力・標準エラーと各試行の終了コード・エンジン名は、起動した Terminal と利用者の `~/.cache/secure-browser/ime.log`（`XDG_CACHE_HOME` 設定時はその配下）に出力されます。

Rocky Linux 8 の IBus 1.5.19 では、`ibus engine` によるエンジン指定がエンジン切り替え後のキーボード配列処理で失敗する場合もあります。終了コードだけで「エンジンが起動していない」と判断してはいけません。上記では `use-system-keyboard-layout` を `true` にして IBus 独自の配列変更を抑え、配列は GNOME の入力ソース設定に任せます。

旧版ラッパーで `KKC did not become ready` だけが出る場合は、管理者へ戻り、この節の作成コマンドでラッパーを上書きしてから Test User で再実行してください。新しいログが `engine=xkb:us::eng` 以外、または終了コードが 0 以外なら、その出力を確認します。旧版（起動時に KKC を選択する版）では、GNOME の表示が `en` でもかな入力になる場合があります。必要に応じて同じ Test User の Terminal で `ibus engine`、`ibus list-engine`、`gsettings get org.freedesktop.ibus.general use-system-keyboard-layout` を実行し、選択中のエンジン、KKC の登録有無、配列設定を切り分けます。IBus に接続できない場合は、Terminal とラッパーでセッション環境が異なる可能性もあるため、ラッパー内のログを優先してください。ユーザー切り替えでの確認は可能ですが、再起動後と Fleet の新規セッションでも別途検証してください。

入力ソースは US 配列の直接入力（既定）と日本語 KKC です。日本語配列を使う場合は `('xkb','us')` を `('xkb','jp')` に、ラッパー内の `xkb:us::eng` を `xkb:jp::jpn` に変更し、配信先で記号キーも確認してください（エンジン名は `ibus list-engine` で確認できます）。アプリケーションをこのラッパーから起動するたびに、英字入力と切り替えキーを設定し直します。そのため、日本語入力中に同じセッションで Chrome や VS Code を新たに起動すると、英字入力へ戻ります。

本構成はアプリケーション配信（`stream_view = "APP"`）のため、利用者には Linux デスクトップ上部の入力ソースメニューが表示されません。入力モードは `Ctrl + Space` で切り替えます。macOS ではこのキーが標準の入力ソース切り替えに割り当てられており、リモートへ届かない場合があります。macOS の利用者には、手元のショートカット割り当てを解除または変更する手順を案内します（利用者マニュアルへの追記事項を参照）。Image Builder のデスクトップ上でメニュー操作ができても、配信先で使える方法とは扱わないでください。

#### 3. Chrome を揮発性の業務用ブラウザに設定する

GitHub とブラウザ内 Codespaces を利用するため、Chrome を継続します。Firefox でも GitHub は利用できますが、[GitHub 公式は非 Chromium 系で Codespaces に問題がある場合に Chrome / Edge への切り替えを案内](https://docs.github.com/en/codespaces/troubleshooting/troubleshooting-github-codespaces-clients)しています。

通常モードを使い、セッション中は Cookie やローカルストレージを利用できるようにします。シークレットモードの強制や Cookie の全面禁止は行いません。ブラウザアカウントのログイン・同期と、GitHub への Web ログインは別です。

[Chrome の Linux 管理ポリシー](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/enterprise/policies.md)をローカルファイルで適用します。Google Workspace やクラウド管理への登録は不要です。

```shell
sudo install -d -o root -g root -m 0755 /etc/opt/chrome/policies/managed
sudo tee /etc/opt/chrome/policies/managed/secure-browser.json > /dev/null <<'EOF'
{
  "BrowserSignin": 0,
  "SyncDisabled": true,
  "PasswordManagerEnabled": false,
  "AutofillAddressEnabled": false,
  "AutofillCreditCardEnabled": false,
  "PromotionalTabsEnabled": false,
  "RestoreOnStartup": 4,
  "RestoreOnStartupURLs": [
    "https://github.com/"
  ]
}
EOF
sudo chown root:root /etc/opt/chrome/policies/managed/secure-browser.json
sudo chmod 0644 /etc/opt/chrome/policies/managed/secure-browser.json

sudo tee /etc/opt/chrome/policies/managed/languages.json > /dev/null <<'EOF'
{
  "ForcedLanguages": ["ja", "en-US", "en"]
}
EOF

sudo chown root:root /etc/opt/chrome/policies/managed/languages.json
sudo chmod 0644 /etc/opt/chrome/policies/managed/languages.json
```

| 設定 | 効果 |
| --- | --- |
| `BrowserSignin: 0` | Chrome 自体への Google アカウントログインを禁止。GitHub などの Web サイトへのログインは可能 |
| `SyncDisabled` | ブラウザ設定・履歴などのクラウド同期を無効化 |
| `PasswordManagerEnabled` | 新規パスワードの保存を無効化。既存の保存済み情報を削除する設定ではない |
| `AutofillAddressEnabled` / `AutofillCreditCardEnabled` | 住所・カード情報の自動入力を無効化 |
| `PromotionalTabsEnabled` | プロモーションタブを抑制 |
| `RestoreOnStartup` / `RestoreOnStartupURLs` | 起動時に GitHub を表示 |

ポリシー自体にプロファイルの破棄機能はありません。揮発性は、ホームフォルダ保存・アプリケーション設定永続化の無効化と、WorkSpaces Applications のセッション終了による端末の破棄で実現します。Chrome ウィンドウを閉じるだけでは、同じセッションの Cookie やプロファイルは削除されません。

##### Chrome はキーリングを使わずに起動する

`Choose password for new keyring` は GitHub や Chrome アカウントのパスワード要求ではなく、GNOME Keyring の初期設定です。Chrome はパスワード保存以外にも Cookie などのローカルデータの保護にキーリングを利用するため、`PasswordManagerEnabled: false` だけでは表示を防げません。利用者に GitHub のパスワードや全員共通のパスワードを設定させないでください。

本環境では組織の方針として、Chrome を `--password-store=basic` 付きで起動し、利用者にキーリングの作成・パスワード管理を求めません。次節のアプリケーションカタログ登録にも、この引数を含めます。OS 全体のキーリングを無効化する設定ではなく、Chrome の保存方式だけを変更します。

[Chromium 公式の説明](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/linux/password_storage.md)では `basic` は暗号化しない保存方式です。キーリングによるパスワード・Cookie などのローカルデータの保護を期待できないことを許容し、パスワード保存禁止ポリシー、プロファイルの非永続化、セッション終了を前提として採用します。ディスク暗号化やセッション終了時の破棄は、実行中に同一ユーザー権限でデータを読まれるリスクの代替対策にはなりません。

この変更は通常の GitHub の Web ログインやブラウザ内 Codespaces の利用を禁止するものではありません。Cookie やローカルストレージはセッション中に利用でき、同じセッション内で Chrome を閉じて開き直しても、キーリングの作成・解除操作は不要です。ただし、終了時に削除される Cookie や認証側の制約により、Web サイトへの再ログインが必要な場合はあります。実際の認証方法（SSO / MFA を含む）と Codespaces の操作は、配信前に検証してください。

既存の登録から変更する場合は、表示中のダイアログをキャンセルし、同じ利用者の Chrome をすべて終了してから、`sudo` を付けずに実行します。既存プロセスが残っていると、新しい起動引数が反映されない場合があります。

```shell
/usr/local/bin/with-japanese-input.sh /usr/bin/google-chrome-stable \
  --password-store=basic \
  --no-first-run --disable-dev-shm-usage --start-maximized
```

`chrome://version` のコマンドラインに `--password-store=basic` があることと、キーリングのダイアログが出ないことを確認します。既存の保存データを変換・削除する手順ではなく、キーリングで保護されていた Cookie などを引き継げるとは限りません。機密情報を含まない新規 Test User / Fleet セッションで検証してください。

#### 4. アプリケーションの登録と Image Builder での確認

Chrome 本体ではなく、日本語入力ラッパーを起動パスとして登録します。既存の `GoogleChrome` / `VSCode` 登録がある場合は、Image Assistant で削除してから登録し直してください。

```shell
AppStreamImageAssistant add-application \
  --name 'GoogleChrome' \
  --display-name 'Google Chrome' \
  --absolute-app-path '/usr/local/bin/with-japanese-input.sh' \
  --launch-parameters '/usr/bin/google-chrome-stable --password-store=basic --no-first-run --disable-dev-shm-usage --start-maximized'
AppStreamImageAssistant add-application \
  --name 'VSCode' \
  --display-name 'Visual Studio Code' \
  --absolute-app-path '/usr/local/bin/with-japanese-input.sh' \
  --launch-parameters '/usr/bin/code --new-window'
```

デスクトップ側に独自のショートカットを置く場合も、同じラッパー経由かつ Chrome には `--password-store=basic` を付けて起動するようにしてください。OS 標準のショートカットや `/usr/bin/google-chrome-stable` の直接起動は、日本語入力の初期化や組織指定の起動引数を適用しません。利用者にはカタログの Google Chrome を案内します。この引数は起動経路の設定であり、別の起動方法まで禁止する強制ポリシーではありません。

Image Builder を再起動し、再接続後に Terminal から確認できます。

```shell
/usr/local/bin/with-japanese-input.sh /usr/bin/google-chrome-stable \
  --password-store=basic \
  --no-first-run --disable-dev-shm-usage --start-maximized
```

- Chrome 起動直後、入力欄に英字がそのまま入力されることを確認します。Image Builder のデスクトップでは入力ソース表示も確認できますが、配信先のアプリケーション画面には表示されません。
- `Ctrl + Space` で日本語 KKC に切り替え、ひらがな入力・`Space` による漢字変換・`Enter` による確定を確認し、英字入力へ戻せることも確認します。macOS から接続する場合は、手元の `Control + Space` の割り当てを先に解除または変更します。デスクトップのメニュー操作ではなく、配信先と同じキー操作で検証してください。
- `chrome://policy` を開き、ポリシーを再読み込みして、上記の各設定が認識されエラーがないことを確認します。
- `chrome://version` で `--password-store=basic` を確認し、初回起動と同一セッション内での Chrome 終了・再起動の両方でキーリングのダイアログが出ないことを確認します。
- Google アカウントへのブラウザログインや同期の案内が抑制され、GitHub への Web ログインと Codespaces のブラウザ内起動はできることを確認します。
- VS Code でもラッパー経由で日本語入力できることを確認します。

GitHub への実ログインはテスト用アカウントで行い、確認後にログアウトしてください。Image Assistant の利用者向け初期設定へ、管理者のブラウザプロファイル・Cookie・パスワード・トークンをコピーしてはいけません。既に認証情報を含むプロファイルをコピーした場合は、イメージ作成前に除去してください。共有設定は管理ポリシーと上記ラッパーで配布します。

#### 5. イメージ作成と配信先での検証

再起動後の Terminal では、Organization 名をもう一度設定してから作成します。

```shell
export APPSTREAM_IMAGE_NAME='APPSTREAM_IMAGE_NAME環境変数の値'
AppStreamImageAssistant create-image --name "${APPSTREAM_IMAGE_NAME}"
```

最後のコマンドを実行すると自動的に接続が切断されて、`${APPSTREAM_IMAGE_NAME}` という名前でイメージが生成されます。更新時は既存名と重複しないイメージ名にし、完成後に `APPSTREAM_IMAGE_NAME` を新しい名前へ変更して Terraform を適用してください。

Image Builder の確認だけでは完了とせず、新イメージを使用する Fleet の新規利用者セッションで、次を確認します。

1. アプリケーションカタログから Chrome を起動し、起動直後は英字入力であること、日本語への切り替え・変換・確定と英字への戻しができる。可能なら Windows と macOS の両方から接続して確認する。
2. `chrome://policy` に設定が適用され、Chrome のアカウントログインは禁止、GitHub の Web ログインは可能である。カタログ起動時の `chrome://version` に `--password-store=basic` があり、初回と Chrome 再起動時にキーリング操作が不要で、組織の SSO / MFA も利用できる。
3. Codespaces をブラウザ内で開き、エディターでの日本語入力、Terminal、保存、再接続が動作する。初期設定や拡張機能の同期も利用しない方針なら、Codespaces / VS Code 側の Settings Sync も別途無効にする。Chrome の `SyncDisabled` では制御できない。
4. テスト用の Cookie・履歴・ローカルファイルを作成して **End Session** を実行する。終了後に新規セッションを開始し、前の利用者データや GitHub のログイン状態が引き継がれない。ただし、外部 IdP の SSO などによる再認証と Cookie の引き継ぎは区別する。
5. 切断して同じセッションへ再接続した場合は状態が残ることを確認し、「切断」と「終了」の案内が実際の動作と一致する。

揮発性だけで情報持ち出し対策は完結しません。必要に応じて Stack のクリップボード・ファイル転送・印刷制御と、Fleet の切断後タイムアウト・アイドルタイムアウトを別途設定し、実際の適用状態を確認してください。これらは上記の Chrome ポリシーでは変更しません。

## 利用者マニュアルへの追記事項

別途作成する利用者マニュアルには、以下を転記してください。新イメージを配信して上記の検証が完了した環境が対象です。

### セキュアブラウザの起動と GitHub / Codespaces の利用

1. ポータルで GitHub 認証を行い、**セキュアブラウザ**を選択します。
2. リモート画面のアプリケーションカタログから **Google Chrome** を起動します。管理者指定の起動方法を使ってください。
3. リモートの Chrome 内で GitHub にログインします。ポータルを開いた手元のブラウザとリモートの Chrome は別の環境なので、GitHub の認証が改めて必要になることがあります。
4. GitHub で対象リポジトリの **Code → Codespaces** から Codespace を選択し、ブラウザ内で開きます。デスクトップ版 VS Code を開く案内が出ても、ブラウザで作業する場合はブラウザ内で開く方法を選んでください。

Chrome 自体への Google アカウントログインやブラウザ同期は使えません。これは想定された設定です。GitHub などの Web サイトへのログインは可能です。Chrome のパスワード保存や住所・カード情報の自動入力も無効です。必要な認証手段は、組織の指定に従って準備してください。

組織の設定により、Chrome 用のキーリングの作成やパスワード入力は不要です。利用者が保存方式を選ぶ必要はありません。必ずカタログの Google Chrome を使用してください。`Choose password for new keyring` が表示された場合は、GitHub のパスワードを入力したり、新しいパスワードを設定したりせず、キャンセルして管理者へ連絡してください。これは OS のキーリング設定であり、GitHub の認証画面ではありません。Chrome のローカルデータはキーリングでは保護しないため、作業終了時は必ず **End Session** を実行してください。

### 日本語入力

セキュアブラウザでは、手元の PC の IME ではなく、**リモートの Linux 上の IME**（日本語かな漢字）を使います。手元の PC の入力モードは英字（直接入力）にしておいてください。手元とリモートの両方で日本語入力が有効だと、正しく入力できません。

#### 起動時の入力モード

Chrome や VS Code を起動した直後は **英字入力**です。GitHub へのログインなどは、そのまま英字で入力できます。日本語入力中に Chrome や VS Code を新たに起動すると、英字入力に戻ります。

#### 入力モードの切り替え（共通）

本環境はアプリケーションのみを配信するため、Linux デスクトップや、その上部の入力ソースメニューは使えません。**キーボードで入力モードを切り替えます。**

1. 変換中の文字があれば `Enter` で確定し、リモートの Chrome や Codespaces の入力欄をクリックします。
2. **`Ctrl + Space`** を 1 回押して両方のキーを離すと、英字入力と日本語入力が切り替わります。もう一度押すと元に戻ります。逆順の切り替えは **`Ctrl + Shift + Space`** です。
3. 入力ソース表示ではなく、入力欄で英字のまま入力されるか、ひらがなになるかを確認します。手元の PC の入力表示は、リモートの入力モードを示すものではありません。

#### Windows からの接続

- 入力欄をクリックしてから **`Ctrl + Space`** を押すと、英字入力と日本語入力が切り替わります。逆順の切り替えは **`Ctrl + Shift + Space`** です。
- 手元の Windows の IME は英字（直接入力）にしておきます。`半角/全角` キーなどは手元の IME の切り替えに使われるため、リモートの入力モードの切り替えには使いません。
- `Ctrl + Space` で切り替わらない場合は、手元の Windows や常駐アプリのショートカットと競合していないか確認します。設定変更が組織の方針で許可されていない場合や、解消しない場合は管理者へ連絡してください。

#### macOS からの接続

macOS では `Control + Space` が標準で手元の入力ソース切り替えに割り当てられているため、手元で処理される場合があります。**リモートのデスクトップメニューは使えないため、Mac 側の割り当てを解除または変更してから利用します。**

1. 手元の Mac の **システム設定 → キーボード → キーボードショートカット → 入力ソース** を開きます。
2. `Control + Space` が割り当てられた項目（「前の入力ソースを選択」など）のチェックを外すか、別のキーに変更します。逆順の切り替えも使う場合は、`Control + Shift + Space` の割り当ても確認してください。
3. 手元の入力ソースを **ABC**（英字）にします。
4. 配信画面の入力欄をクリックして **`Control + Space`** を押し、英字入力と日本語入力が切り替わることを確認します。`Command + Space` ではありません。

この変更は Mac 全体に影響します。手元の入力切り替えには、英数／かなキーなど別の方法を使ってください。設定変更が組織の方針で許可されていない場合や、変更後もリモートで切り替えられない場合は、管理者へ連絡してください。

#### 日本語の入力

- 日本語入力ではローマ字でひらがなを入力し、**`Space`** で漢字候補へ変換、**`Enter`** で確定します。
- 入力モードを切り替える前に、変換中の文字は `Enter` で確定してください。
- 日本語入力ができない場合は、入力欄にフォーカスを置き、`Ctrl + Space` で切り替えてから入力を確認します。手元の OS の切り替え画面や Codespaces の補完候補が開く場合は、キー操作がリモートの IME 切り替えに届いていない可能性があります。上記の OS ごとの設定を確認し、解消しない場合は管理者へ連絡してください。
- 記号キーの入力がずれる場合は、キーボード配列の問題の可能性があります。使用しているキーボードが日本語配列か US 配列かを管理者へ伝えてください。

### 保存と終了

- セキュアブラウザ内のダウンロード、ローカルファイル、Chrome / ローカル VS Code の設定は、次の新規セッションに引き継がれません。終了前に、必要な成果物を GitHub や組織が許可した保存先へ保存してください。
- **Codespaces のファイルはリモート Linux 端末とは別の場所にあります。** エディターの保存を確認し、共有・保全する変更は commit / push してください。Codespace の停止・削除や GitHub 上のデータ管理は別途必要です。
- 作業終了時は、WorkSpaces Applications のメニューから **End Session** を実行します。Chrome ウィンドウや配信タブを閉じるだけでは終了になりません。切断中も、セッションが終了するまではログイン状態などが残る場合があります。
- 終了後に「セッションを終了しました」と表示されることを確認してください。「再度ログインする」から新しい利用を開始できます。
- **End Session は、手元のブラウザの GitHub ログアウトや Codespaces の停止・削除を行いません。** 共有 PC では手元の GitHub からもログアウトし、必要に応じて Codespace を停止してください。ブラウザデータの破棄だけで GitHub 側の認可が取り消されるわけでもありません。
- ブラウザの同期禁止と、Codespaces / VS Code の Settings Sync は別です。開発環境の設定同期については組織の方針に従ってください。
- ファイル転送・クリップボード・印刷は、管理者の設定により制限される場合があります。利用できない場合は、許可された保存・共有方法を確認してください。

# イメージのアップデート手順

## 1. Image Builder に管理者で接続する

1. Amazon WorkSpaces Applications の **Images → Image Builder** で、イメージ作成時に使用した Image Builder を開きます。名前は作成時の設定を確認してください（例：`image-builder-${BUSTION_GITHUB_ORGANIZATION}` または `image-builder-linux-${BUSTION_GITHUB_ORGANIZATION}`）。
2. Status が **Stopped** の場合は **Actions → Start** で起動し、**Running** になるまで待ちます。
3. **Connect** で接続し、**ImageBuilderAdmin** として操作します。Chrome と VS Code を終了し、**Applications → Terminal** を開きます。

以下の更新コマンドは Image Builder 内で実行します。途中でエラーが出た場合はイメージを作成せず、原因を解消してください。

## 2. OS・Chrome・VS Code 本体を更新する

```shell
sudo dnf upgrade --refresh -y
rpm -q google-chrome-stable code
```

`dnf upgrade`（`dnf update` と同義）は OS パッケージに加え、有効なリポジトリにある Chrome と VS Code も更新します。VS Code の拡張機能は対象外です。

Chrome または VS Code が更新されない場合は、リポジトリと更新候補を確認します。最新版が既に導入済みの場合、更新候補は表示されません。

```shell
dnf repolist --enabled
dnf list --showduplicates google-chrome-stable code
```

初回手順の Chrome RPM は通常 Google のリポジトリを登録します。Google のリポジトリがない場合は、次の公式 RPM から更新できます。VS Code のリポジトリがない場合は「アプリケーションと日本語 IME のインストール」のリポジトリ設定を再実行してください。

```shell
sudo dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
```

## 3. VS Code の拡張機能を更新して利用者へ配布する

**ImageBuilderAdmin の Terminal で、`code` コマンドには `sudo` を付けずに**実行します。

```shell
code --list-extensions --show-versions
code --update-extensions
code --install-extension GitHub.codespaces --force
code --list-extensions --show-versions
```

`--update-extensions` は、この管理者のローカル VS Code にインストールされた拡張機能を更新します。`GitHub.codespaces` は初回手順で必須としているため、未導入の場合も含めてインストールします。追加の共通拡張機能が必要な場合も、同じユーザーで `code --install-extension publisher.extension` を実行してください。

更新後は VS Code を終了し、表示された拡張機能がすべて全利用者への配布対象であることを確認します。管理者で更新しただけでは、新規利用者には反映されません。初回手順と同様に `/etc/skel/.vscode/extensions` に配置し直します。

次の処理は **スケルトン内の既存の共通拡張機能をすべて置き換えます**。スケルトンにだけある必要な拡張機能は、先に管理者にもインストールしてください。古いバージョンや削除済みの拡張機能を残さないため、単なる上書きコピーではなく置き換えます。

```shell
(
    set -e
    test -d "$HOME/.vscode/extensions"
    sudo mkdir -p /etc/skel/.vscode
    if [ -e /etc/skel/.vscode/extensions ]; then
        sudo rm -r -- /etc/skel/.vscode/extensions
    fi
    sudo mkdir -p /etc/skel/.vscode/extensions
    sudo cp -r "$HOME/.vscode/extensions/." /etc/skel/.vscode/extensions/
    sudo chown -R root:root /etc/skel/.vscode/extensions
    sudo chmod -R a+rX /etc/skel/.vscode/extensions
)
code --extensions-dir /etc/skel/.vscode/extensions --list-extensions --show-versions
```

管理者側と配布先の拡張機能の一覧・バージョンが一致することを確認します。Chrome や VS Code のユーザープロファイル、Cookie、認証情報はコピーしないでください。

スケルトンはユーザー作成時に適用されるため、既に作成済みの Test User や既存 Fleet セッションのホームには遡って反映されません。最終確認は新イメージを使う Fleet の新規セッションで行ってください。また、Codespaces 内のリモート拡張機能やブラウザ版 VS Code の拡張機能は、この更新の対象外です。

## 4. 再起動・動作確認後にイメージを作成する

1. AWS コンソールから Image Builder を再起動し、**Running** に戻ったら再接続します。
2. カタログから Chrome と VS Code を起動し、バージョン、日本語入力、Chrome のポリシー・優先言語・キーリング不要の動作、VS Code の拡張機能と Codespaces 接続を確認します。詳細は「アプリケーションの登録と Image Builder での確認」を参照してください。
3. テスト用アカウントをログアウトし、Chrome と VS Code を終了します。
4. 管理者の Terminal で、一意な名前を指定してイメージを作成します。同日に再作成する場合も重複しないよう連番などを付けてください。

```shell
export APPSTREAM_IMAGE_NAME='実際のイメージ名-YYYYMMDD-01'
AppStreamImageAssistant create-image --name "${APPSTREAM_IMAGE_NAME}"
```

作成処理で接続が切断されます。**Images → Image Registry** を **Visibility = Private** で絞り込み、新イメージが **Available** になるまで待ってください。コマンド実行直後の切断や Image Builder の停止だけでは、イメージの完成確認になりません。

## 5. Fleet のイメージを切り替えて確認する

1. メンテナンス時間を案内し、利用者に保存と **End Session** を依頼します。作業中は新規接続も止めてください。
2. **Fleets → `fleet-bustion-${BUSTION_GITHUB_ORGANIZATION}`** を開き、**In Use = 0** を確認してから **Actions → Stop** を実行し、**Stopped** になるまで待ちます。イメージ変更より先に停止します。
3. **Image → Edit** で新しい `${APPSTREAM_IMAGE_NAME}` を選択し、**Save Changes** を実行します。
4. Codespaces Secret の `APPSTREAM_IMAGE_NAME` も同じ新イメージ名に変更します。既存 Codespace の環境変数には自動反映されないため、Terraform を実行する Terminal でも更新してください。コンソールだけ変更すると、次回の Terraform 適用で旧イメージへ戻る可能性があります。

   リポジトリ直下の Terminal で実行します（Image Builder 内ではありません）。

   ```shell
   export APPSTREAM_IMAGE_NAME='実際のイメージ名-YYYYMMDD-01'
   export TF_VAR_appstream_image_name="$APPSTREAM_IMAGE_NAME"
   python3 scripts/login.py
   source ~/.config/aws-bustion/terraform-env.sh
   ```

   次回の Terraform 実行前には `terraform plan` でイメージ名が旧名に戻らないことも確認してください。

5. **Actions → Start** を実行し、**Running** になるまで待ちます。
6. Fleet の新規利用者セッションで、Chrome・VS Code 本体と拡張機能のバージョン、優先言語、日本語入力、GitHub / Codespaces 接続、キーリング不要の動作、**End Session** 後のデータ破棄を確認します。利用者側の VS Code Terminal では `code --list-extensions --show-versions` で配布された拡張機能を確認できます。
7. 問題がなければ利用を再開します。問題がある場合は同じ停止・変更・起動手順で旧イメージへ戻し、Codespaces Secret と Terraform 用の環境変数も旧名に揃えます。検証が完了するまでは旧イメージを削除しないでください。
