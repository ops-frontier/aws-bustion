# GitHub OAuth App の準備

この手順は GitHub OAuth App の登録情報を用意するものです。**現在の Terraform / AppStream 構成には OAuth 連携が実装されていないため、登録した App だけでは AppStream にサインインできません。** GitHub OAuth と AppStream の間に置く認証ブローカー / SAML IdP とその callback URL を選定してから、本番用 OAuth App を完成させてください。

## 1. 認証ブローカーを決める

GitHub OAuth App の Authorization callback URL は、OAuth 応答を受け取るアプリケーションの URL です。現状このリポジトリには callback を受け取るサービスがありません。README に記載されていた Cognito User Pool の `/oauth2/idpresponse` URL を指定しないでください。Cognito User Pool はその URL で GitHub OAuth の汎用プロバイダーにはならず、AppStream 向け SAML アサーションの発行者にもなりません。

GitHub OAuth を受け付け、必要に応じて GitHub 組織メンバーシップを検証し、AppStream がサポートする SAML サインインを開始できるブローカーを選定します。ブローカーの設定後、管理者から次の値を受け取ってください。

- OAuth App の Homepage URL
- OAuth App の Authorization callback URL
- 組織メンバーシップ検証に必要な OAuth scope とトークン管理方針

これらの URL や scope はブローカーに依存するため、このリポジトリでは固定値を定義していません。

## 2. OAuth App を作成する

1. GitHub で **Settings → Developer settings → OAuth Apps → New OAuth App** を開きます。Organization 管理者が管理する場合は、組織のポリシーに従ってください。
2. **Application name** に用途が分かる名前を入力します。
3. **Homepage URL** に、選定したブローカーの URL を入力します。
4. **Authorization callback URL** に、ブローカーが指定した callback URL を入力します。ローカル検証と本番では URL が異なる場合があるため、環境ごとの OAuth App を分けることを検討してください。
5. 作成後に Client ID を記録し、**Generate a new client secret** から Client Secret を発行します。Secret は安全な Secrets 管理先に保管し、リポジトリや README に記載しないでください。

ブローカーと callback URL が未決定の場合は、仮の Cognito URL で OAuth App を作成せず、値が決まってから登録してください。

## 3. 組織メンバーシップ確認

プライベートな GitHub Organization のメンバーシップを確認する場合、ブローカーが要求する最小権限を選びます。OAuth App で `read:org` scope が必要となる構成では、その権限が求められる理由とトークンの保存・更新・失効方法を確認してください。利用者に表示される認可画面で、要求する scope を必要最小限にします。

README の旧サンプルにあった Lambda の Pre Sign-up / Pre Authentication コードはこのリポジトリには実装されていません。GitHub API の確認だけを追加しても AppStream への SAML ログインが成立するわけではありません。また、PAT を Codespaces Secrets に登録する手順はありません。

## 4. Client ID / Secret の保管

現在の `.devcontainer/devcontainer.json` と Terraform は `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` を参照しません。認証ブローカー実装後、ブローカーの実行環境が要求する Secrets 名と保管先に登録してください。Codespaces Secrets に登録するだけでは Terraform や AppStream に連携されません。

Client Secret が漏えいした場合は OAuth App の設定から再発行し、ブローカー側の Secret を更新した後、古い Secret を失効させてください。