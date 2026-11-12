# NodeSeek 自动签到

只依赖 Cookie 的 NodeSeek 每日签到，支持 GitHub Actions 和 VPS（systemd）。签到后查询近 30 天收益，并可通过 Telegram 推送结果。

## 使用方式（GitHub Actions）

1. 将本目录推送到你的 GitHub 仓库。
2. 打开仓库 `Settings > Secrets and variables > Actions`。
3. 在 `Secrets` 中新增 `NS_COOKIE`，值填写 NodeSeek 登录后的完整 Cookie。
4. 打开仓库 `Actions > NodeSeek Signin > Run workflow` 手动测试一次。

workflow 只支持手动触发（`workflow_dispatch`），不会按计划自动运行；需要每天定时签到时用 VPS 部署。

## 获取 Cookie

登录 NodeSeek 后，打开浏览器开发者工具，在 Network 面板里点任意 `www.nodeseek.com` 请求，复制 Request Headers 里的 `Cookie` 内容。

Cookie 等同于登录凭证，只放到 GitHub Secrets 或 VPS 的 `.env`，不要提交到代码仓库。

## 多账号

`NS_COOKIE` 支持多个 Cookie，用换行或 `&` 分隔：

```text
cookie_of_account_1
&
cookie_of_account_2
```

不要用分号分隔多个账号，因为单个 Cookie 本身就包含分号。VPS 的 `.env` 不支持多行，直接用 `&` 连接。

## 可选配置

GitHub Actions 里放在 `Settings > Secrets and variables > Actions`；VPS 部署写在项目根目录的 `.env` 中，变量名相同：

| 名称 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `NS_COOKIE` | Secret | 必填 | NodeSeek Cookie，支持多账号 |
| `NS_PROXY` | Secret | 空 | 访问 NodeSeek 的 HTTP/SOCKS 代理，VPS IP 被拦截时用 |
| `NODE_LINK` | Secret | 空 | 仅 VPS：VLESS + XHTTP 节点链接，配置后自动起 Xray |
| `NS_PROXY_PORT` | Variable | `1082` | 仅 VPS：`NODE_LINK` 的本地代理端口 |
| `XRAY_PATH` | Variable | 空 | 仅 VPS：Xray 可执行文件路径，默认从 PATH 找 |
| `NS_TIMEOUT` | Variable | `60` | 单次请求超时秒数 |
| `NS_RANDOM` | Variable | `true` | 是否启用随机签到 |
| `FAIL_ON_ERROR` | Variable | `true` | 任一账号失败时是否让任务失败 |
| `NS_IMPERSONATE` | Variable | `chrome136` | `curl_cffi` 使用的浏览器指纹 |
| `TG_BOT_TOKEN` | Secret | 空 | Telegram Bot Token |
| `TG_USER_ID` | Secret | 空 | Telegram 接收者 ID |
| `TG_THREAD_ID` | Secret | 空 | Telegram 群组话题 ID |
| `TG_API_HOST` | Variable | `https://api.telegram.org` | Telegram API 代理地址 |

## VPS 部署

要求 Python 3.10+ 和 systemd，不依赖浏览器或 Docker。

### 1. 放代码

放在任意目录即可，配置和虚拟环境都跟着项目走：

```bash
git clone <你的仓库地址> ~/nodeSeek-signin
cd ~/nodeSeek-signin
```

### 2. 一键安装

```bash
sudo bash deploy/install.sh
```

脚本就地安装：`.env` 和 `.venv` 都在项目目录下，systemd 里写的是这个目录的绝对路径。运行用户默认取执行 `sudo` 的用户（直接用 root 跑时用 `nodeseek`），可用 `SERVICE_USER=xxx` 覆盖。安装后启用 `nodeseek-signin.timer`：每天北京时间 `08:20` 触发，随机延迟 0–10 分钟。

### 3. 配置 Cookie

推荐用脚本写入，自动替换 `NS_COOKIE` 并设好权限：

```bash
sudo bash deploy/set-cookie.sh
```

按提示粘贴 Cookie（输入不回显），回车确认。

也可以手动编辑项目目录下的 `.env`：

```bash
sudo nano ~/nodeSeek-signin/.env
```

把 `NS_COOKIE=` 后面改成从浏览器复制的 Cookie，只占一行；多账号用 `&` 连接：

```bash
NS_COOKIE=account1_cookie&account2_cookie
```

Cookie 从哪来：登录 NodeSeek，在浏览器开发者工具 Network 面板点任意 `www.nodeseek.com` 请求，复制 Request Headers 里的 `Cookie` 内容。Cookie 只保存在服务器本地 `.env`，脚本不会上传到别处。

### 4. 启动并手动验证

首次安装时 `.env` 是空的，定时器只 enable 未启动，填好配置后执行：

```bash
sudo systemctl start nodeseek-signin.timer
sudo systemctl start nodeseek-signin.service
journalctl -u nodeseek-signin.service -n 100 --no-pager
systemctl list-timers nodeseek-signin.timer
```

### 5. 更新代码

```bash
cd ~/nodeSeek-signin
sudo git pull
sudo .venv/bin/pip install -r requirements.txt
```

timer 下次触发就用新代码，不需要重启服务。

### Cookie 过期怎么办

重新从浏览器复制 Cookie，在项目目录执行 `sudo bash deploy/set-cookie.sh` 覆盖即可。下次 timer 触发自动读取新值，不用重启服务；想立刻验证就 `sudo systemctl start nodeseek-signin.service`。

### 节点代理（NODE_LINK，VLESS + XHTTP）

VPS 的 IP 被 Cloudflare 拦时，可以让签到请求走你的节点。只支持 VLESS + XHTTP，后端只依赖 Xray：

```bash
sudo bash deploy/install-xray.sh
# 或者在安装时一起装：
# sudo bash deploy/install.sh --with-xray
```

然后在 `.env` 里填节点链接：

```dotenv
NODE_LINK=vless://uuid@host:443?type=xhttp&security=reality&pbk=公钥&fp=chrome&sni=域名&sid=shortid&path=%2F
# 可选，默认 1082
NS_PROXY_PORT=1082
# 可选，Xray 不在 PATH 时指定
# XRAY_PATH=/usr/local/bin/xray
```

运行逻辑：

- 配置 `NODE_LINK` 后，脚本启动 Xray，在 `127.0.0.1:1082` 开 HTTP 代理，签到和收益统计都走这个节点，任务结束后自动退出。
- `NODE_LINK` 优先于 `NS_PROXY`；两者都配时用节点。
- 支持 `type=xhttp` 和 `type=splithttp`；`ws` / `grpc` 不支持。
- 链接里的 UUID 等同密码，`.env` 保持 `600`，不要提交到仓库。

只检查链接解析（输出含密钥，别外传）：

```bash
.venv/bin/python nodeseek_proxy.py --link 'vless://...' --print-config
```

### VPS 上容易踩的坑

- **IP 被 Cloudflare 拦**：签到接口返回 403 时，在 `.env` 配 `NODE_LINK`（VLESS + XHTTP）或 `NS_PROXY`，签到和统计请求都会走对应出口。
- **`.env` 是 systemd 格式**：不支持多行值，多账号 Cookie 用 `&` 连接；值里有空格或引号时用单引号包住整个值。
- **Telegram 不通**：国内 VPS 直连 `api.telegram.org` 会超时，用 `TG_API_HOST` 换成自己的反代。
- **时区**：timer 已固定 `Asia/Shanghai`，不用改服务器时区；脚本统计日期同样按北京时间。
- **失败处理**：`FAIL_ON_ERROR=true` 时任一账号失败服务会标记失败，只想记日志就设 `false`。
- **依赖**：只有 `curl_cffi`，独立 venv 约几十 MB；想和其他 Python 项目共用，安装时传 `VENV_DIR=/path/to/venv`。
- **项目目录**：systemd 里记录的是安装时的绝对路径，之后移动目录要重新执行 `deploy/install.sh`；不要把项目放在 `/root` 下，运行用户读不到。需要换运行用户时传 `SERVICE_USER=xxx`。

## 本地检查

```bash
python -m pip install -r requirements.txt
NS_COOKIE="你的 Cookie" python nodeseek_signin.py
```
