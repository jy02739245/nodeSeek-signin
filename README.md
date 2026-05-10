# NodeSeek GitHub Actions 签到

这是一个只保留 GitHub Actions 定时运行的 NodeSeek 签到项目。优先使用 Cookie 签到，Cookie 失效时可用账号密码 + `cf-clearance-scraper` 自动登录，并把新 Cookie 写回 GitHub Secret。

## 使用方式

1. 将本目录推送到你的 GitHub 仓库。
2. 打开仓库 `Settings > Secrets and variables > Actions`。
3. 在 `Secrets` 中新增 `NS_COOKIE`，值填写 NodeSeek 登录后的完整 Cookie。如果你只想用账号密码首次自动登录，也可以先不填 Cookie，但必须配置下面的自动刷新变量。
4. 打开仓库 `Actions > NodeSeek Signin > Run workflow` 手动测试一次。

workflow 已配置每天北京时间 `08:20` 自动运行。GitHub Actions 的 cron 使用 UTC，所以 `.github/workflows/nodeseek-signin.yml` 中的 `20 0 * * *` 对应北京时间 `08:20`。

## 获取 Cookie

登录 NodeSeek 后，打开浏览器开发者工具，在 Network 面板里点任意 `www.nodeseek.com` 请求，复制 Request Headers 里的 `Cookie` 内容。

Cookie 等同于登录凭证，只放到 GitHub Secrets，不要提交到代码仓库。

## Cookie 失效自动刷新

如果希望 Cookie 失效后自动用账号密码登录并更新 `NS_COOKIE`，继续添加这些 `Secrets`：

| 名称 | 必要性 | 说明 |
| --- | --- | --- |
| `USER` | 必填 | NodeSeek 用户名 |
| `PASS` | 必填 | NodeSeek 密码 |
| `CF_SOLVER_URL` | 必填 | 你部署的 `cf-clearance-scraper` 服务 URL，例如 `https://example.com` |
| `GH_PAT` | 必填 | 用于更新仓库 Secret 的 GitHub PAT |
| `CF_SOLVER_AUTH_TOKEN` | 可选 | 只有你的 `cf-clearance-scraper` 服务启用了 `authToken` 时才需要 |

`cf-clearance-scraper` 默认接口是 `POST /cf-clearance-scraper`。你只需要填服务根地址，脚本会自动拼接接口路径。

`GH_PAT` 推荐使用 fine-grained token，只给当前仓库权限，并开启 `Secrets` 的 read/write 权限。Classic token 可用 `repo` scope。

## 多账号

`NS_COOKIE` 支持多个 Cookie，用换行或 `&` 分隔：

```text
cookie_of_account_1
&
cookie_of_account_2
```

不要用分号分隔多个账号，因为单个 Cookie 本身就包含分号。

多账号自动刷新时，按顺序配置 `USER1`/`PASS1`、`USER2`/`PASS2`，最多默认读取到 `USER10`/`PASS10`。如果只配置一个账号，也可以直接用 `USER`/`PASS`。

## 可选配置

这些配置放在 `Settings > Secrets and variables > Actions` 中：

| 名称 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `NS_COOKIE` | Secret | 建议 | NodeSeek Cookie，支持多账号 |
| `USER`/`PASS` | Secret | 空 | Cookie 失效时用于自动登录 |
| `USER1`/`PASS1`... | Secret | 空 | 多账号自动登录配置 |
| `CF_SOLVER_URL` | Secret | 空 | `cf-clearance-scraper` 服务地址 |
| `CF_SOLVER_AUTH_TOKEN` | Secret | 空 | 可选的 solver 服务认证 token |
| `GH_PAT` | Secret | 空 | 自动更新 `NS_COOKIE` Secret 所需的 GitHub PAT |
| `TG_BOT_TOKEN` | Secret | 空 | Telegram Bot Token |
| `TG_USER_ID` | Secret | 空 | Telegram 接收者 ID |
| `TG_THREAD_ID` | Secret | 空 | Telegram 群组话题 ID |
| `NS_RANDOM` | Variable | `true` | 是否启用随机签到 |
| `FAIL_ON_ERROR` | Variable | `true` | 任一账号失败时是否让 Actions 失败 |
| `NS_IMPERSONATE` | Variable | `chrome136` | `curl_cffi` 使用的浏览器指纹 |
| `CF_SOLVER_MODE` | Variable | `turnstile-min` | 可改为 `turnstile-max` 做完整页面解析 |
| `GITHUB_COOKIE_SECRET_NAME` | Variable | `NS_COOKIE` | 写回的 GitHub Secret 名称，通常不用改 |
| `SAVE_COOKIE_TO_GITHUB` | Variable | `true` | 是否把自动登录获取的新 Cookie 写回 GitHub Secret |
| `TG_API_HOST` | Variable | `https://api.telegram.org` | Telegram API 代理地址 |

## 本地检查

```bash
python -m pip install -r requirements.txt
NS_COOKIE="你的 Cookie" python nodeseek_signin.py
```
