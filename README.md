# aws-bution
Amazon Workspaces Application 上で踏み台端末を提供するIaCです。踏み台端末へのログインは Github OAuth で認証連携し、特定のGithub組織に所属しているユーザが利用できます。

# IaC

構築には Terraform と Packer を使用します。AWS CLI で MFA を使用してプロファイルを切り替えて必要なロールを割り振ります。terraform や packer のコマンド実行時に OTP を入力することで必要な権限を持ったロールに切り替わるようにします。AWS でのアクセスキー、ロール、MFAの作成手順については、 AWS-SETUP.md に記載する。Githubの OAuthの設定手順については GITHUB-SETUP.md に記載する。
.devcontainer/devcontainer.json で AWS CLI をインストールし、~/.aws に環境変数をもとに必要な情報を作成します。postCreateCommand は post-create.sh というシェルスクリプトに外出しする。

## 環境変数

IaC の認証のために以下の環境変数を Codespaces の Secrets から設定していただきます。

|環境変数名|説明|例|
|--|--|--|
|GITHUB_ORGANIZATION| 対象となる Github の組織名 | ops-frontier |
|GITHUB_CLIENT_ID| Github OIDC の連携に使用するクライアントID| k7RwCq5t9dsn2bYx |
|GITHUB_CLIENT_SECRET| Github OIDC の連携に使用するクライアントシークレット| k7RwCq5t9dsn2bYx |
|AWS_ACCESS_KEY_ID|AWS アクセスキーのID| k7RwCq5t9dsn2bYx |
|AWS_SECRET_ACCESS_KEY|AWS アクセスキーの値| k7RwCq5t9dsn2bYx |
|AWS_ROLE_ARN| AWS のロールARN | arn:aws:iam::123456789012:role/dev-admin |
|AWS_SERIAL_NUMBER| AWS に登録したMFAのARN | arn:aws:iam::123456789012:mfa/dev-admin |

## AWS のリソース名

Terraform で作成する AWS のリソース名には "-bustion-${GITHUB_ORGANIZATION}" という文字列を付与する。例えば、Githubの組織名が "ops-frontier" の場合 VPC の名前は vpc-bustion-ops-frontier となる。
Terraform で環境変数を参照するために TF_VAR_ の接頭辞で変数名を小文字化したものに転記するように .devcontainer/devcontainer.json を設定する。

# AWSサービスの選択
AWS で踏み台として使用できる仮想端末サービスには、Amazon WorkSpaces、Amazon AppStream 2.0、Amazon WorkSpaces Secure Browserの三つがあります。
- Amazon WorkSpacesは個人ごとに端末を割り当てるためのサービスであり、開発者が開発環境にアクセスするときのみ使えれば良いという今回の要求仕様に対してオーバスペックである。
- Amazon WorkSpaces Secure Browserは 2026年10月末でサービスを終了するため使用できない。
したがって、Amazon AppStream 2.0を使用します。ただし、Amazon AppStream 2.0 は Amazon Workspaces Application に名前を変えていますので、このドキュメントでは、以降 Amazon Workspaces Application と呼びます。

# Windows vs Linux
Windows では RDS SAL（ユーザー単位の月額ライセンス費用：約 $4.19 /人）が月に1回でも接続すると課金されるので、 Linux を使用します。ただし、将来的に USBパスキーを使用する必要が出た場合には Windows 版の作成を検討します。

# VPC
この IaC で VPC を作成します。
最終的に Github の Allow IP List を使用して踏み台経由のアクセス以外のアクセスを禁止することを考えています。 このため、VPC はプライベートサブネットを2個で構成し、NATゲートウェイ経由でインターネットにアクセスするようにします。NAT ゲートウェイはゾーンごとのものではなく、リージョナルを使用します。
ただし、Allow IP List の導入には、様々な課題があります。今回はそれらを考慮せずに IP 制限がないものとして構築します。

# イメージ
この IaC で Amazon Workspaces Application のイメージを作成します。イメージには日本語の言語パック、Chrome, VSCodeをインストールします。

## Packer によるビルド
AppStream 2.0 のイメージ実体は 「AppStream 用のエージェントやドライバが組み込まれた EC2 AMI」 です。そのため、Packer で EC2 インスタンスを一時起動してプロビジョニングし、AMI として保存・登録するフローを構築します。
[Packer (amazon-ebs)]
  │
  ├─ 1. AWS 公式の AppStream ベース AMI (Rocky Linux 8) から EC2 起動
  ├─ 2. SSH / SSM 経由でプロビジョニング実行 (Ansible / Shell)
  │      ├─ 日本語フォント、Chrome、VS Code のインストール
  │      └─ AppStreamImageAssistant によるアプリ登録 & 最適化
  ├─ 3. AMI (Amazon Machine Image) のキャプチャ・保存
  └─ 4. (AWS CLI / Lambda) 保存された AMI を AppStream イメージとして登録

### 具体的なコード実装例 (HCL2)
以下は、Rocky Linux 8 ベースの AppStream イメージを生成する Packer テンプレート（build.pkr.hcl）の実装例です。
```
Terraform
packer {
  required_plugins {
    amazon = {
      version = ">= 1.2.0"
      source  = "github.com/hashicorp/amazon"
    }
    ansible = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/ansible"
    }
  }
}

source "amazon-ebs" "appstream_image" {
  region        = "ap-northeast-1"
  instance_type = "stream.standard.medium" # または t3.medium
  
  # 1. AWS 公式の AppStream Linux ベース AMI を検索・指定
  source_ami_filter {
    filters = {
      name                = "AppStream-RockyLinux8-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["amazon"] # AWS 公式 AMI
  }

  ssh_username = "ec2-user"
  ami_name     = "Linux-Chrome-VSCode-Image-{{timestamp}}"

  # VPC / サブネットの設定
  subnet_id = "subnet-xxxxxxxxxxxxxxxxx"
}

build {
  sources = ["source.amazon-ebs.appstream_image"]

  # 2. シェルスクリプトや Ansible で OS・アプリをプロビジョニング
  provisioner "shell" {
    inline = [
      # 日本語言語パックのインストール
      "sudo dnf install -y langpacks-ja",

      # Chrome のインストール
      "sudo dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm",

      # VS Code のインストール
      "sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc",
      "echo -e '[code]\nname=Visual Studio Code\nbaseurl=https://packages.microsoft.com/yumrepos/vscode\nenabled=1\ngpgcheck=1\ngpgkey=https://packages.microsoft.com/keys/microsoft.asc' | sudo tee /etc/yum.repos.d/vscode.repo",
      "sudo dnf install -y code",

      # URL スキームの紐付け (vscode://)
      "xdg-mime default code.desktop x-scheme-handler/vscode"

      # デフォルトユーザー (ec2-user) 向けに拡張機能をインストール 
       "code --install-extension ms-vscode.github-codespaces --force", 
      # 新しく作成される全ユーザーに拡張機能を適用するため、スケルトンディレクトリ (~/.vscode/extensions) にコピー 
      "sudo mkdir -p /etc/skel/.vscode/extensions", 
       "sudo cp -r ~/.vscode/extensions/* /etc/skel/.vscode/extensions/ || true"
    ]
  }

  # 3. AppStream 専用 CLI (AppStreamImageAssistant) でアプリを登録 & 最適化準備
  provisioner "shell" {
    inline = [
      # Chrome アプリの追加
      "AppStreamImageAssistant add-application --name 'GoogleChrome' --display-name 'Google Chrome' --absolute-app-path '/usr/bin/google-chrome-stable' --launch-parameters '--no-first-run --disable-dev-shm-usage --start-maximized'",

      # VS Code アプリの追加
      "AppStreamImageAssistant add-application --name 'VSCode' --display-name 'Visual Studio Code' --absolute-app-path '/usr/bin/code' --launch-parameters '--new-window'",

      # イメージ作成前の最適化・クリーンアップ処理の実行
      "AppStreamImageAssistant prepare-environment"
    ]
  }
}
```

### 実装・運用時の重要な注意点・ノウハウ

#### AppStreamImageAssistant prepare-environment の実行
通常コンソールから Image Builder を操作する際に行われる「イメージの最適化（アプリケーションプロファイル・起動最適化）」処理に相当する処理を、プロビジョニングの最終ステップとして CLI で呼び出す必要があります。

#### AppStream イメージカタログへの反映（create-image） 
Packer が出力するのは「EC2 の AMI」です。この AMI を AppStream 2.0 の管理コンソール（および CLI）上の正式な「AppStream Image」としてリスト表示・利用可能にするには、ビルド後に AWS CLI から AppStream 用のイメージ登録を行うか、Packer の post-processor（または CI/CD パイプラインの後続ステップ）で aws appstream create-image を呼び出すスクリプトを連携させます。

#### Ansible 併用によるクリーンなコード化
上記例の provisioner "shell" の部分は、そのまま provisioner "ansible-local" や ansible に差し替え可能です。OS 設定、VS Code 拡張機能のプリインストール、ユーザー権限周りの複雑な構成管理は Ansible Playbook に任せるとメンテナビリティが高くなります。

###  補足：GIthub Web からの VSCode 起動
github.com 上で "Open in Visual Studio Code" を押した際、ブラウザ（Chrome）は vscode:// というカスタム URL スキームを呼び出します。これを Linux OS 上で受けるために以下が必要です。

1. VS Code に「GitHub Codespaces 拡張機能」がインストールされていること
VS Code 単体では Codespaces のコンテナにリモート接続できません。
Image Builder 上の VS Code で GitHub Codespaces（ms-vscode.github-codespaces）拡張機能 をあらかじめインストールしておく必要があります。

2. Linux OS 側に URL スキーム（vscode://）のハンドラーが登録されていること
Chrome が vscode:// を叩いた時に「OS の VS Code アプリを立ち上げる」ための定義（MIME タイプ連携）が必要です。
通常、公式 .rpm パッケージから dnf install code でインストールした場合は自動登録されますが、登録されていない場合は Image Builder 上で以下の設定（.desktop ファイルのハンドラー登録）を行います。
Bash
# URLスキーム（vscode://）を VS Code に紐付けるコマンド
xdg-mime default code.desktop x-scheme-handler/vscode

3. GitHub の認証（ログイン状態）が VS Code 側でも完了していること
VS Code が起動した後、初めて Codespaces に接続するタイミングで GitHub へのログインを求められます。
VS Code の画面左下（人型アイコン）から GitHub アカウントへ一度ログインしておく必要があります。

注意点：動作を安定させるための「プロファイル永続化」
前述の通り、AppStream 2.0 は使い捨て（エフェメラル）環境です。
せっかく VS Code に 「Codespaces 拡張機能」を入れたり、「GitHub ログイン」を行ったりしても、ユーザーがログアウトすると設定が初期化されてしまい、次回ログイン時に再び "Open in Visual Studio Code" を押してもエラーになるか、再度ログインを求められてしまいます。
【対策】
スタックの Application Settings Persistence（アプリケーション設定の維持） を有効にし、以下のフォルダがログアウト時に S3 に保存されるように設定してください。
~/.vscode/ （インストールした拡張機能）
~/.config/Code/ （VS Code の設定・認証トークン）
~/.local/share/applications/ （URLスキームの紐付け定義）

# フリート
マルチユーザ・マルチセッションのフリートを作成します。

## スケール設定の検討
昼間に最大 20 セッション（20 ユーザー同時接続）を想定し、「Chrome (GitHub) ＋ VS Code (Codespaces 接続)」を快適かつ最小コストで運用するためのインスタンスサイズの選定とオートスケール設定（ベストプラクティス）を設計・ご提案します。

### 1. リソース要件の見積もり（1 ユーザーあたり）
まず、指定されたワークロードに必要な 1 ユーザーあたりのリソースを見積もります。

Chrome（GitHub Web UI）: 約 0.8 〜 1.2 GB RAM / 0.5 vCPU 相当

VS Code（Codespaces 接続のみ）:
※重いビルドや言語サーバー処理はクラウド側の Codespaces コンテナで実行されるため、手元の VS Code（AppStream 上）はUI レンダリングと SSH/Websocket 通信メインの軽量な動きになります。
$\rightarrow$ 約 0.5 〜 0.8 GB RAM / 0.2 〜 0.3 vCPU 相当

OS ＋ AppStream エージェント基本消費: 約 1.0 GB RAM

👉 結論: 1 ユーザーあたり 「1.5 〜 2.0 GB RAM / 0.5 vCPU」 を確保できれば非常に快適に動作します。

### 2. 推奨インスタンスサイズの比較と選定
マルチユーザーセッションでは、1 台の大型インスタンスを集約して使う方がコスト効率が良くなります。

構成案
インスタンスタイプ
スペック (vCPU / RAM)
1台あたりの推奨収容人数
20セッションに必要な台数
コスト効率と推奨度
案 A (推奨)
stream.standard.xlarge
4 vCPU / 16 GB
7 〜 8 人
約 3 台
★★★★★ (ベスト)


コスト・障害分離のバランスが最も優秀

案 B
stream.memory.large
2 vCPU / 15.25 GB
6 〜 7 人
約 3 台
★★★★☆


CPU 負荷が極めて低い場合に有効

案 C
stream.standard.2xlarge
8 vCPU / 32 GB
15 〜 16 人
約 2 台
★★★☆☆


集約率は高いが、1台障害時の影響範囲が大きい

🏆 最適解：stream.standard.xlarge (4 vCPU / 16 GB)
収容目安: 1 台あたり 7 ユーザー (合計メモリ消費: OS 1GB + 7人 × 2GB = 15GB $\to$ メモリ使用率 約 93% で安全圏)。

20 ユーザー到達時には 3 台 のインスタンスで安定してさばくことができます。

### 3. 最適なオートスケール・フリート設定
急なログイン集中（朝の始業時など）でユーザーが待たされるのを防ぎつつ、夜間の無駄なコストを抑える設定値です。

フリート基本設定
Max User Sessions per Instance (1台あたりの最大セッション数): 7

Fleet Type: ON_DEMAND (または業務時間固定なら ALWAYS_ON)

Minimum Capacity (最小インスタンス数): 1 (夜間・休日用)

Maximum Capacity (最大インスタンス数): 4 (20〜25セッションの突発増加に対応可能な上限)

スケーリングポリシー設計（2 段階ルール）
AppStream 2.0 の自動スケール（起動）には 1〜2 分のタイムラグ があるため、「常に 1 台分の余裕（空き枠）」を維持するルール を組むのが鉄則です。

ルール①: スケールアウト (増設ルール)
指標 (Metric): AvailableCapacity (すぐに接続できる空きセッション数)

条件: AvailableCapacity が 3 セッション以下 になった場合

アクション: インスタンスを 1 台追加

挙動: 1 台目（収容上限7）に 4 人目がログインした時点で、空き枠が 3 になるため先行して 2 台目が起動開始します。これにより、5〜7 人目がログインしている間に 2 台目の準備が完了し、8 人目のユーザーは待たずに即座に接続できます。

ルール②: スケールイン (削減ルール)
指標 (Metric): CapacityUtilization (利用率)

条件: CapacityUtilization が 30% 未満 の状態が 15 分継続した場合

アクション: インスタンスを 1 台削減

### 4. 日時スケジュールスケール（さらにコストを絞る場合）
昼間の混雑時間帯が「平日 9:00 〜 18:00」と決まっている場合は、上記のアラームベースのスケーリングに加え、Scheduled Scaling（スケジュール設定）を組み合わせるのが最も効果的です。

平日 8:30 (始業直前):
Minimum Capacity を 2 に引き上げ（事前に 2 台 = 14 セッション分を温めておく）。

平日 19:00 (終業後):
Minimum Capacity を 1 (または 0) に戻す。

これで「朝一で全員が一斉にログインして重くなる・待たされる」という問題を完全に回避できます。

#  スタック
ユーザが Chrome/ VSCode にアクセスできるようにスタックを作成します。
スタックでは、ホームディレクトリが S3 に自動的に保存されるように設定します。

# 認証

Cognito を GitHub OAuth と連携させ、その結果で AppStream 2.0 (WorkSpaces Applications) の Stack にログインさせるためには、IAM の Identity Provider (SAML プロバイダ) として Cognito を登録し、Cognito 経由でログインしたユーザーが Assume (引き受け) するための IAM ロールを作成する必要があります。

この構成の全体像（アーキテクチャ）と、それを一括で構築するための Terraform コード例 を解説します。

全体アーキテクチャと処理の流れ
[ ユーザー ]
    │
    │ 1. ログイン開始（Cognito Hosted UI / SAML Endpoint）
    ▼
[ GitHub.com ] ──( 2. OAuth 2.0 認証成功 )──► [ Amazon Cognito ]
                                                   │
                                                   │ 3. SAML アサーション (XML) を生成
                                                   ▼
                                          [ AWS IAM (SAML Provider) ]
                                                   │
                                                   │ 4. sts:AssumeRoleWithSAML
                                                   ▼
                                          [ IAM Role (AppStreamAccessRole) ]
                                                   │
                                                   │ 5. appstream:Stream 許可
                                                   ▼
                                          [ AppStream 2.0 Stack ]

GitHub OAuth アプリ: ユーザーの本人確認と OAuth 認可を行います。

Cognito ユーザープール: GitHub から OAuth トークンを受け取り、事前に定義した SAML 属性（Role や RoleSessionName）を含んだ SAML アサーション (XML) に中継・変換します。

IAM SAML プロバイダ: Cognito から送られてきた SAML アサーションを検証します。

IAM ロール: SAML 認証を通過したセッションに対し、対象の AppStream スタックへストリーミング接続するための appstream:Stream 権限を一時的に付与します。

## Terraform コード例
以下は、Cognito ユーザープール、SAML IdP 設定、IAM SAML プロバイダ、IAM ロール、およびそれらの連携に必要な属性マッピングを一括定義する Terraform コードです。

``` Terraform
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  default = "ap-northeast-1"
}

variable "github_client_id" {
  type        = string
  description = "GitHub OAuth App Client ID"
}

variable "github_client_secret" {
  type        = string
  sensitive   = true
  description = "GitHub OAuth App Client Secret"
}

variable "appstream_stack_name" {
  type        = string
  default     = "Linux-Chrome-Stack"
  description = "Target AppStream Stack Name"
}

data "aws_caller_identity" "current" {}

# ------------------------------------------------------------------------------
# 1. Amazon Cognito ユーザープール & ドメインの作成
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool" "appstream_user_pool" {
  name = "appstream-github-user-pool"
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "appstream-auth-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.appstream_user_pool.id
}

# ------------------------------------------------------------------------------
# 2. GitHub OAuth プロバイダの追加 (Cognito 側)
# ------------------------------------------------------------------------------
resource "aws_cognito_identity_provider" "github" {
  user_pool_id  = aws_cognito_user_pool.appstream_user_pool.id
  provider_name = "GitHub"
  provider_type = "Custom" # GitHub は OIDC/OAuth Custom として登録

  provider_details = {
    client_id                 = var.github_client_id
    client_secret             = var.github_client_secret
    authorize_scopes          = "user:email"
    attributes_request_method = "GET"
    oidc_issuer               = "https://github.com"
    authorize_url             = "https://github.com/login/oauth/authorize"
    token_url                 = "https://github.com/login/oauth/access_token"
    attributes_url            = "https://api.github.com/user"
  }

  attribute_mapping = {
    email    = "email"
    username = "id"
  }
}

# ------------------------------------------------------------------------------
# 3. Cognito の SAML 2.0 IdP 機能用メタデータプレースホルダー定義
# ------------------------------------------------------------------------------
# Cognito の SAML メタデータ URL:
# https://cognito-idp.<region>.amazonaws.com/<user_pool_id>/.well-known/saml-metadata.xml

data "http" "cognito_saml_metadata" {
  url = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.appstream_user_pool.id}/.well-known/saml-metadata.xml"

  depends_on = [
    aws_cognito_user_pool.appstream_user_pool
  ]
}

# ------------------------------------------------------------------------------
# 4. IAM SAML プロバイダの作成
# ------------------------------------------------------------------------------
resource "aws_iam_saml_provider" "cognito_saml" {
  name                   = "CognitoAppStreamSAMLProvider"
  saml_metadata_document = data.http.cognito_saml_metadata.response_body
}

# ------------------------------------------------------------------------------
# 5. Assume (引き受け) 用 IAM ロールの作成
# ------------------------------------------------------------------------------
# SAML アサーションに基づいて引き受けを許可する信頼関係ポリシー
data "aws_iam_policy_document" "appstream_saml_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithSAML"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_saml_provider.cognito_saml.arn]
    }

    condition {
      string_equals = {
        "SAML:aud" = "https://signin.aws.amazon.com/saml"
      }
    }
  }
}

resource "aws_iam_role" "appstream_saml_role" {
  name               = "AppStreamCognitoAccessRole"
  assume_role_policy = data.aws_iam_policy_document.appstream_saml_assume_role.json
}

# AppStream スタックへのアクセスを許可する IAM ポリシー
resource "aws_iam_policy" "appstream_stream_policy" {
  name        = "AppStreamStreamAccessPolicy"
  description = "Allows streaming session start for AppStream"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "appstream:Stream"
        ]
        Resource = "arn:aws:appstream:${var.region}:${data.aws_caller_identity.current.account_id}:stack/${var.appstream_stack_name}"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "attach_appstream" {
  role       = aws_iam_role.appstream_saml_role.name
  policy_arn = aws_iam_policy.appstream_stream_policy.arn
}

# ------------------------------------------------------------------------------
# 6. Cognito App Client & SAML 属性マッピングの設定
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool_client" "appstream_client" {
  name         = "appstream-saml-client"
  user_pool_id = aws_cognito_user_pool.appstream_user_pool.id

  supported_identity_providers = ["GitHub"]

  # SAML 連携用の Callback URL
  callback_urls = [
    "https://signin.aws.amazon.com/saml"
  ]

  allowed_oauth_flows                  = ["code", "implicit"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
}
```

## 構築・運用のポイント
GitHub OAuth Callback URL:
GitHub 側の Developer Settings で指定する Callback URL には、Cognito ユーザープールのドメイン（例: https://appstream-auth-<ACCOUNT_ID>[.auth.ap-northeast-1.amazoncognito.com/oauth2/idpresponse](https://.auth.ap-northeast-1.amazoncognito.com/oauth2/idpresponse)）を設定します。

SAML 属性（Role 属性）の紐付け:
Cognito から発行される SAML レスポンスには、以下の形式で Role 属性が含まれる必要があります：
arn:aws:iam::<ACCOUNT_ID>:role/AppStreamCognitoAccessRole,arn:aws:iam::<ACCOUNT_ID>:saml-provider/CognitoAppStreamSAMLProvider
（IAM ロール ARN と SAML プロバイダ ARN をカンマ区切りで指定）

特定ユーザーの制限:
誰でも GitHub アカウントでログインできてしまうのを防ぐため、必要に応じて Cognito 側で Pre Sign-up Lambda Trigger を記述し、許可された GitHub ユーザーID や組織（Organization）のみにサインアップを絞り込みます。

## GitHub 組織に所属しているかチェック
Cognito の **Pre Sign-up（サインアップ前）Lambda トリガー** を使用して、初回サインオン時に GitHub API を呼び出し、ログインしようとしているユーザーが指定の GitHub 組織（Organization）に所属しているかチェックする Python コード例と設定手順を解説します。

---

### 全体の仕組み

1. ユーザーが GitHub アカウントで初めてログインを試みると、Cognito はユーザーを作成する直前に Pre Sign-up Lambda トリガーを発火させます。
2. Lambda はイベントに含まれる **GitHub のユーザー名（または ID）** を取得します。
3. GitHub REST API（`GET /orgs/{org}/members/{username}`）を実行し、該当ユーザーが組織のメンバーであるか判定します。
4. **組織メンバーの場合:** `event['response']['autoConfirmUser'] = True` などを設定して正常終了（レスポンス返却）し、ユーザー作成を許可します。
5. **組織外のユーザーの場合:** Exception (エラー) を発生させることで、Cognito は**ユーザー作成および AppStream へのアクセスを拒否**します。

---

### Python (Boto3 / urllib) コード例

外部ライブラリ（`requests` 等）を使わず、Python 標準ライブラリの `urllib.request` のみで動くように作成しています（Lambda Layer の準備が不要です）。

```python
import json
import urllib.request
import urllib.error
import os

# 環境変数から設定値を取得
ALLOWED_GITHUB_ORG = os.environ.get('GITHUB_ORG_NAME', 'your-github-org')
# GitHub API のレート制限回避およびプライベート組織の確認用 Personal Access Token (PAT)
GITHUB_PAT = os.environ.get('GITHUB_PAT', '')

def lambda_handler(event, context):
    print("Received event:", json.dumps(event))
    
    # トリガーソースの確認（外部 IdP 経由の初回サインアップ時）
    trigger_source = event.get('triggerSource', '')
    
    if trigger_source == 'PreSignUp_ExternalProvider':
        # Cognito の userName 形式は通常 "GitHub_<github_user_id>" または "GitHub_<username>"
        user_name_raw = event.get('userName', '')
        
        # ユーザー名からプレフィックス (GitHub_) を除去
        if '_' in user_name_raw:
            github_username = user_name_raw.split('_', 1)[1]
        else:
            github_username = user_name_raw
            
        print(f"Validating GitHub user '{github_username}' against Org '{ALLOWED_GITHUB_ORG}'")

        # GitHub API URL (組織のメンバーシップ確認)
        url = f"https://api.github.com/orgs/{ALLOWED_GITHUB_ORG}/members/{github_username}"
        
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'AWS-Cognito-PreSignUp-Trigger')
        req.add_header('Accept', 'application/vnd.github+json')
        
        if GITHUB_PAT:
            req.add_header('Authorization', f'Bearer {GITHUB_PAT}')

        try:
            with urllib.request.urlopen(req) as response:
                # 204 No Content が返ってきた場合は組織のメンバー
                if response.status in (204, 200):
                    print(f"SUCCESS: User {github_username} is a member of {ALLOWED_GITHUB_ORG}.")
                    # 外部認証ユーザーを自動承認
                    event['response']['autoConfirmUser'] = True
                    event['response']['autoVerifyEmail'] = True
                    return event
                    
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 404 Not Found: ユーザーは指定組織のメンバーではない
                print(f"DENIED: User {github_username} is NOT a member of {ALLOWED_GITHUB_ORG}.")
                raise Exception(f"Access Denied: You must be a member of the GitHub organization '{ALLOWED_GITHUB_ORG}' to access AppStream.")
            elif e.code == 302:
                # 302: 2要素認証が必要な組織などの理由でメンバーシップが非公開になっている場合
                print(f"DENIED: Member status is private or requires auth.")
                raise Exception(f"Access Denied: Could not verify membership in '{ALLOWED_GITHUB_ORG}'.")
            else:
                print(f"GitHub API Error: {e.code} - {e.reason}")
                raise Exception("Authentication failed due to an error validating GitHub organization membership.")
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            raise Exception("Internal server error during authentication.")

    # その他のサインアップ要求はそのまま通過（または必要に応じて制御）
    event['response']['autoConfirmUser'] = True
    return event

```

---

### 設定手順（Terraform / AWS コンソール）

#### 1. GitHub 側で Token (PAT) を準備（推奨）

対象の GitHub Organization が**プライベート組織**の場合、またはメンバーシップ設定を「Private」にしているユーザーを正しく判定するには、組織の読み取り権限を持った GitHub Token が必要です。

* **Fine-grained Personal Access Token (PAT):**
`Resource Owner` に対象の Organization を指定し、Organization Permissions で **`Members: Read-only`** を付与した Token を発行します。

#### 2. Lambda 関数の作成と環境変数の設定

* **Runtime:** Python 3.11 または 3.12
* **環境変数 (Environment Variables):**
* `GITHUB_ORG_NAME`: `your-company-org`（判定対象の GitHub 組織名）
* `GITHUB_PAT`: `github_pat_xxxxxx`（手順 1 で取得した Token）



#### 3. Cognito ユーザープールヘの権限付与（Resource Policy）

Cognito から Lambda を呼び出せるように、Lambda 側にパーミッションを追加します。

```bash
aws lambda add-permission \
  --function-name "arn:aws:lambda:ap-northeast-1:123456789012:function:CognitoPreSignUpCheck" \
  --statement-id "CognitoAllowInvoke" \
  --action "lambda:InvokeFunction" \
  --principal "cognito-idp.amazonaws.com" \
  --source-arn "arn:aws:cognito-idp:ap-northeast-1:123456789012:userpool/ap-northeast-1_XXXXXXX"

```

#### 4. Cognito ユーザープールにトリガーを登録

* **AWS コンソールの場合:**
Cognito User Pool ＞ **Extensions（拡張機能）** タブ ＞ **Lambda triggers** ＞ **Sign-up（サインアップ）** 内の **Pre sign-up trigger（サインアップ前トリガー）** に作成した Lambda 関数を登録します。
* **Terraform の場合:**
`aws_cognito_user_pool` リソースの `lambda_config` ブロックで指定します。

```hcl
resource "aws_cognito_user_pool" "pool" {
  name = "appstream-user-pool"

  lambda_config {
    pre_sign_up = aws_lambda_function.pre_signup_check.arn
  }
}

```

---

### 動作テストと注意事項

1. **初回ログイン時のみ発火:**
Pre Sign-up トリガーは「Cognito ユーザープールにそのユーザーが初めて作成される瞬間」にのみ実行されます。2回目以降のログインでは発火しません。
2. **組織から除外されたユーザーの即時アクセス遮断:**
すでに一度ログインして Cognito にアカウントが作られた後に GitHub 組織から退職等で除外されたユーザーのアクセスを止める場合は、定期的に Cognito ユーザープール側から削除するバッチ処理を作成するか、**Pre Authentication（認証前）トリガー** にも同様のロジックを組み込んで毎回検証します。

## 2回目以降のログイン時のチェック

Pre Authentication（認証前）トリガーを実装することで、初回サインアップ時だけでなく **2回目以降の毎回のログイン時** にも GitHub API をリアルタイムで呼び出し、退職やプロジェクト離脱等で GitHub 組織（Organization）から外れたユーザーのアクセスを即座に遮断することができます。

---

### 全体の動作フロー

1. ユーザーが AppStream へログインを試みる（GitHub OAuth 認証完了後）。
2. Cognito はトークンを発行してログインを完了させる直前に **Pre Authentication Lambda** を実行します。
3. Lambda はユーザーの識別子（GitHub ユーザー名）を取得し、GitHub REST API（`GET /orgs/{org}/members/{username}`）を呼び出します。
4. **組織に所属中（200 / 204）:** Lambda は正常終了し、Cognito は認証を許可して AppStream へリダイレクトします。
5. **組織から除外済み（404）:** Lambda が例外（Exception）を発生させ、Cognito は **ログイン処理を中断（拒否）** します。

---

### Python (Boto3 / urllib) コード例

Pre Sign-up 用とほぼ共通のロジックですが、Pre Authentication イベント構造に合わせてユーザー名の取得部分を調整しています。標準ライブラリのみで動作します。

```python
import json
import urllib.request
import urllib.error
import os

# 環境変数から設定値を取得
ALLOWED_GITHUB_ORG = os.environ.get('GITHUB_ORG_NAME', 'your-github-org')
GITHUB_PAT = os.environ.get('GITHUB_PAT', '')

def lambda_handler(event, context):
    print("Received PreAuthentication event:", json.dumps(event))
    
    # Cognito ユーザー名を取得
    # 外部 IdP 経由のユーザー名形式例: "GitHub_12345678" または "GitHub_username"
    user_name_raw = event.get('userName', '')
    
    # 属性（email / preferred_username / identities）から GitHub ユーザー名の取得を試みる
    user_attributes = event.get('request', {}).get('userAttributes', {})
    github_username = None

    # identities 属性（JSON文字列）がある場合はパースして検索
    if 'identities' in user_attributes:
        try:
            identities = json.loads(user_attributes['identities'])
            for identity in identities:
                if identity.get('providerName') == 'GitHub':
                    github_username = identity.get('userId')
                    break
        except Exception as e:
            print(f"Failed to parse identities attribute: {e}")

    # identities から取れなかった場合は userName から抽出
    if not github_username:
        if '_' in user_name_raw:
            github_username = user_name_raw.split('_', 1)[1]
        else:
            github_username = user_name_raw

    print(f"Validating GitHub user '{github_username}' for Organization '{ALLOWED_GITHUB_ORG}'")

    # GitHub REST API による組織メンバーシップ検証
    url = f"https://api.github.com/orgs/{ALLOWED_GITHUB_ORG}/members/{github_username}"
    
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'AWS-Cognito-PreAuth-Trigger')
    req.add_header('Accept', 'application/vnd.github+json')
    
    if GITHUB_PAT:
        req.add_header('Authorization', f'Bearer {GITHUB_PAT}')

    try:
        with urllib.request.urlopen(req) as response:
            # 204 No Content (または 200) は組織のメンバーであることを示す
            if response.status in (204, 200):
                print(f"SUCCESS: User '{github_username}' is active in '{ALLOWED_GITHUB_ORG}'. Allowing authentication.")
                return event

    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"DENIED: User '{github_username}' is NO LONGER a member of '{ALLOWED_GITHUB_ORG}'.")
            raise Exception(f"Access Denied: You are not a member of the GitHub organization '{ALLOWED_GITHUB_ORG}'.")
        elif e.code == 302:
            print(f"DENIED: Member status requires authentication or is private.")
            raise Exception(f"Access Denied: Unable to verify membership in '{ALLOWED_GITHUB_ORG}'.")
        else:
            print(f"GitHub API Error: {e.code} - {e.reason}")
            raise Exception("Authentication failed due to an error checking organization membership.")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise Exception("Internal server error during authentication check.")

    # 念のため予期せぬ通過を防ぐ
    raise Exception("Authentication denied: Could not verify identity.")

```

---

### 設定方法

#### 1. Lambda パーミッションの付与

Cognito が Pre Authentication トリガーとして Lambda を実行できるようにアクセス許可を与えます。

```bash
aws lambda add-permission \
  --function-name "arn:aws:lambda:ap-northeast-1:123456789012:function:CognitoPreAuthCheck" \
  --statement-id "CognitoAllowInvokePreAuth" \
  --action "lambda:InvokeFunction" \
  --principal "cognito-idp.amazonaws.com" \
  --source-arn "arn:aws:cognito-idp:ap-northeast-1:123456789012:userpool/ap-northeast-1_XXXXXXX"

```

#### 2. Cognito ユーザープールヘの登録

* **AWS コンソールの場合:**
Cognito User Pool ＞ **Extensions（拡張機能）** タブ ＞ **Lambda triggers** ＞ **Pre authentication trigger（認証前トリガー）** に作成した Lambda 関数を指定します。
* **Terraform の場合 (`aws_cognito_user_pool`):**
`lambda_config` に `pre_authentication` を追加します（Pre Sign-up と併用可能です）。

```hcl
resource "aws_cognito_user_pool" "pool" {
  name = "appstream-user-pool"

  lambda_config {
    pre_sign_up        = aws_lambda_function.pre_signup_check.arn
    pre_authentication = aws_lambda_function.pre_auth_check.arn
  }
}

```

---

### 運用上の注意点（ベストプラクティス）

1. **API レート制限対策 (GitHub PAT):**
全ユーザーがログインする度に GitHub API を叩くため、認証なしリクエストの制限（50回/時）に引っかからないよう、必ず `GITHUB_PAT`（Personal Access Token）を設定してください。
2. **Pre Sign-up との役割分担:**
* **Pre Sign-up:** 初回の Cognito アカウント自動作成を制御（未所属ユーザーのアカウント作成を防止）。
* **Pre Authentication:** 既存アカウントの毎回のログインを制御（離脱ユーザーのリアルタイム遮断）。
両方を組み合わせて設定しておくことで、完全なアクセス制御を実現できます。