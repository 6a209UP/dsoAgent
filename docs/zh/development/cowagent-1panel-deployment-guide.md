# CowAgent 在 1Panel 中的部署教程

> 更新时间：2026-09-19  
> 适用范围：当前 CowAgent 仓库及 Source2 医疗客服集成功能  
> 1Panel 基线：V2 版本界面  
> 宿主机基线：CentOS Stream release 10 (Coughlan)  
> 示例域名：`ai.example.com`，部署时必须替换为真实域名

## 1. 部署目标

本教程只部署 CowAgent，不包含 source2、MySQL、Redis、Nacos、管理端或患者端的部署。

推荐结构：

```text
Source2 / 管理员浏览器
          │ HTTPS 443
          ▼
 ai.example.com
          │
 1Panel OpenResty
          │ 127.0.0.1:19899
          ▼
 CowAgent 容器:9899
          │
          ├─ /chat、/assets/*              内置 Web 前端
          ├─ /api/*                        后端接口
          ├─ /app/config.json             只读配置
          └─ /home/agent/cow              持久化工作区
                └─ tenants/<tenantId>     每租户独立数据
```

CowAgent 本身不需要 MySQL 或 Redis。Source2 医疗语料、请求归档、防重放状态和幂等锁保存在各租户独立
工作区；工作区必须持久化、备份并限制访问。

CowAgent 的 Web 前端已经包含在后端源码和镜像中，由同一个 `9899` 端口提供。默认部署不要创建独立前端
静态站点，也不要用单独前端 ZIP 覆盖 `/chat` 或 `/assets`；OpenResty 只需把整个域名反向代理到 CowAgent。

1Panel V2 支持从“容器 → 编排”通过编辑器、服务器路径或编排模板部署 Compose，官方操作说明见
[1Panel 容器编排](https://1panel.cn/docs/v2/user_manual/containers/compose/)。本教程中的镜像构建、编排生成、
启停、日志查看和镜像导出均以 1Panel 页面为主，不要求在服务器终端手工执行 `docker build`、`docker push`、
`docker pull` 或 `docker compose up`。

## 2. 当前仓库的两个重要差异

### 2.1 必须构建自定义镜像

仓库根目录的 `Dockerfile` 只有：

```dockerfile
FROM ghcr.io/zhayujie/chatgpt-on-wechat:latest
ENTRYPOINT ["/entrypoint.sh"]
```

它不会把当前仓库中的 Source2 医疗接口、租户隔离、安全校验和界面修改复制进镜像。因此生产部署必须使用
`docker/Dockerfile.latest` 从当前源码构建，不能直接使用：

- `ghcr.io/zhayujie/chatgpt-on-wechat:latest`；
- `zhayujie/chatgpt-on-wechat`；
- 仓库 `docker/docker-compose.yml` 的默认镜像配置。

### 2.2 生产环境使用不可变标签

不要使用 `latest`。使用发布日期加 Git 提交号，例如：

```text
zhuojian/cowagent:20260919-abcdef
```

该标签由 1Panel 在当前服务器本地构建。每次上线保留旧标签，并从 1Panel 导出一份镜像归档；回滚时选择旧
标签，不重新构建同名镜像。

## 3. 准备服务器和 1Panel

最低建议：

- CentOS Stream release 10 (Coughlan)，x86_64 或 arm64；
- 2 核 CPU、4 GiB 内存；
- 独立数据盘或至少 50 GiB 可用空间；
- 已安装 1Panel V2、Docker 和 OpenResty；
- 一个已解析到服务器的域名，如 `ai.example.com`；
- 服务器时间已通过 NTP 同步。

CentOS Stream 10 默认安全基线按 SELinux Enforcing 和 firewalld 启用设计。不要通过 `setenforce 0` 或关闭
firewalld 规避挂载、端口或代理问题；本教程的 Compose 会对 CowAgent 私有挂载设置 SELinux `Z` 标签。
1Panel 使用 Docker 管理这些容器，不要用 Podman 或 `podman-docker` 混合接管同一组编排。

医疗 HMAC 请求允许的时间偏差有限，服务器时间漂移会直接导致鉴权失败。

公网只开放 80/443。容器端口 9899 不对公网开放，映射为宿主机回环地址的 19899。Docker 发布端口可能
使用独立转发链路，1Panel 官方建议同时检查系统防火墙、Docker 端口防护和云安全组，参见
[1Panel 防火墙](https://1panel.cn/docs/v2/user_manual/hosts/firewall/)。

在“应用商店”安装 OpenResty后，1Panel 的“网站”功能可以管理反向代理和 HTTPS。官方说明见
[OpenResty 应用](https://1panel.cn/docs/v2/user_manual/appstore/openresty/)和
[创建网站](https://1panel.cn/docs/v2/user_manual/websites/website_create/)。

## 4. 使用 1Panel 生成 CowAgent 镜像

本教程采用“上传发布源码 → 1Panel 构建镜像”的方式，不配置外部镜像仓库，也不在终端手工构建、推送或
拉取镜像。1Panel V2 的“容器 → 镜像 → 构建镜像”支持选择服务器上的 Dockerfile，并支持填写构建参数，
官方说明见 [1Panel 镜像](https://1panel.cn/docs/v2/user_manual/containers/image/)。建议使用 1Panel `2.0.13`
或更高版本；旧版本如果没有“构建参数”输入框，先升级面板。

### 4.1 上传干净的发布源码

1. 在受控构建机从已通过发布门禁的提交生成源码压缩包；不要包含 `.git`、`config.json`、`.env`、日志、
   缓存、工作区或任何真实密钥。
2. 进入 1Panel“主机 → 文件”，创建目录
   `/opt/zhuojian/build/cowagent-20260919-abcdef`，上传并解压到该目录。
3. 解压后确认源码根目录直接包含 `app.py`、`config-template.json`、`requirements.txt`、`docker/` 等内容，
   不能多套一层同名目录。
4. 在源码根目录创建 `Dockerfile.1panel`，内容完整复制自本次源码的 `docker/Dockerfile.latest`。

第 4 步不能省略。1Panel“路径选择”会把 Dockerfile 所在目录作为构建上下文；如果直接选择
`docker/Dockerfile.latest`，构建上下文只有 `docker/` 子目录，Dockerfile 中的 `ADD . /app` 无法加入完整
CowAgent 源码。`Dockerfile.1panel` 必须位于源码根目录，并与本次发布的 `docker/Dockerfile.latest` 保持一致。

### 4.2 在 1Panel 构建

进入“容器 → 镜像 → 构建镜像”，按下表填写：

| 1Panel 字段 | 值 |
| --- | --- |
| 名称 | `zhuojian/cowagent:20260919-abcdef` |
| Dockerfile 来源 | `路径选择` |
| Dockerfile | `/opt/zhuojian/build/cowagent-20260919-abcdef/Dockerfile.1panel` |
| 构建参数 | 每行一个：`INSTALL_BROWSER=false`、`USE_CN_MIRROR=true` |
| 标签 | 可填发布号、提交号和责任人等普通标签；不得填写密钥 |

其中：

- `INSTALL_BROWSER=false`：医疗客服不需要 Playwright/Chromium，可减小镜像并减少无关工具面；
- `USE_CN_MIRROR=true`：中国大陆服务器可使用镜像源；境外或受控制品源环境改为 `false`；
- 名称中的日期和提交号必须替换为本次真实发布号，禁止使用 `latest`。

点击“确认”后保持构建任务日志打开，直到任务成功。关闭抽屉后，可在“主机 → 文件”查看
`[1Panel 安装目录]/1panel/tmp/docker_logs/image_build_<时间戳>.log`。构建完成后在镜像列表确认新标签存在，
并记录镜像 ID、创建时间和大小。

### 4.3 从 1Panel 导出发布镜像

在“容器 → 镜像”找到刚构建的固定标签，执行“导出”，将生成的 `.tar` 文件转移到受控备份位置并计算
SHA-256。导出文件用于单机回滚或灾备恢复；不要删除上一版镜像，直到新版本通过灰度和备份恢复验证。

源码留在生产服务器会扩大攻击面。构建和镜像导出完成后，可删除 1Panel 中的构建任务日志及不再需要的
发布源码，但必须保留发布号、Git 提交号、镜像 ID、导出文件 SHA-256 和对应 Dockerfile 的审计记录。

## 5. 创建目录

在 1Panel 终端执行：

```bash
install -d -m 700 /opt/zhuojian/cowagent
install -d -m 700 /opt/zhuojian/cowagent/data
install -d -m 700 /opt/zhuojian/secrets
```

目录用途：

| 路径 | 内容 | 是否备份 |
| --- | --- | --- |
| `/opt/zhuojian/cowagent/config.json` | Agent、模型、Web、Source2 绑定 | 是，必须加密 |
| `/opt/zhuojian/cowagent/data` | 工作区、语料、会话、医疗归档 | 是，必须加密 |
| `/opt/zhuojian/secrets/cowagent.env` | HMAC 和数据密钥环境变量 | 优先由 KMS 托管 |

不要把 `/opt/zhuojian/cowagent/data` 放在临时目录、容器可写层或会被 1Panel“清理未使用卷”删除的位置。

## 6. 创建 CowAgent 配置

### 6.1 普通 Web Agent 配置

仅使用 CowAgent Web 控制台、暂不接 Source2 时，创建
`/opt/zhuojian/cowagent/config.json`：

```json
{
  "cow_lang": "zh",
  "channel_type": "web",
  "web_host": "0.0.0.0",
  "web_port": 9899,
  "web_password": "REPLACE_WITH_A_LONG_RANDOM_PASSWORD",
  "agent": true,
  "agent_workspace": "/home/agent/cow/default",
  "self_evolution_enabled": false,
  "open_ai_api_base": "https://approved-model.example.com/v1",
  "open_ai_api_key": "REPLACE_WITH_MODEL_API_KEY",
  "model": "approved-model-name",
  "bot_type": "openai"
}
```

说明：

- 容器内必须监听 `0.0.0.0`，宿主机仍只映射到 `127.0.0.1`；
- `web_password` 必须使用独立高强度随机值；
- `agent_workspace` 位于持久化挂载目录内；
- 生产环境建议保持 `self_evolution_enabled=false`；
- 模型 API 地址、模型名称和数据处理条款必须经过批准。

当前 `config.py` 会在 INFO 日志中输出通用配置环境变量的覆盖值，因此在修复该日志行为前，不要通过
`OPEN_AI_API_KEY` 环境变量注入模型密钥。上例使用权限为 `0600` 的只读配置文件，启动后仍需检查日志中
不存在模型 Key。

### 6.2 Source2 医疗客服配置

接入 source2 时，在同一个 `config.json` 使用每租户独立 Agent 和绑定。单租户完整示例：

```json
{
  "cow_lang": "zh",
  "channel_type": "web",
  "web_host": "0.0.0.0",
  "web_port": 9899,
  "web_password": "REPLACE_WITH_A_LONG_RANDOM_PASSWORD",
  "agent": true,
  "self_evolution_enabled": false,
  "open_ai_api_base": "https://approved-model.example.com/v1",
  "open_ai_api_key": "REPLACE_WITH_MODEL_API_KEY",
  "model": "approved-model-name",
  "bot_type": "openai",
  "default_agent_id": "source2-tenant-1001",
  "agents": [
    {
      "id": "source2-tenant-1001",
      "name": "机构1001智能客服",
      "workspace": "/home/agent/cow/tenants/1001",
      "model": "approved-model-name",
      "bot_type": "openai",
      "enabled": true
    }
  ],
  "source2_integrations": [
    {
      "enabled": true,
      "tenant_id": "1001",
      "agent_id": "source2-tenant-1001",
      "app_key": "source2-1001",
      "require_external_secrets": true,
      "app_secret_env": "SOURCE2_1001_APP_SECRET",
      "admin_app_key": "source2-admin-1001",
      "admin_app_secret_env": "SOURCE2_1001_ADMIN_APP_SECRET",
      "admin_enabled": true,
      "active_data_key_id": "kms/source2/1001/v1",
      "data_keys": {
        "kms/source2/1001/v1": {"env": "SOURCE2_1001_DATA_KEY_V1"}
      },
      "media_host_allowlist": ["api.example.com"],
      "media_content_type_allowlist": ["image/*", "audio/*", "application/pdf"],
      "model_profile": "medical-commercial-prod",
      "compliance_approved": true,
      "compliance_approval_ref": "PRIVACY-2026-001"
    }
  ]
}
```

每增加一个租户，都必须新增：

- 独立 `agents[]` 项；
- 不重叠的工作区；
- 独立业务 App Key/Secret；
- 独立归档管理员 App Key/Secret；
- 独立数据密钥；
- 对应的合规审批引用。

不同租户不能共用数据密钥、工作区或 App Key。医疗媒体白名单只填写 source2 一次性媒体代理的正式主机名，
不能填写通配符或任意公网下载域名。

## 7. 创建 Source2 密钥环境文件

不接 Source2 时可以跳过本节，并在 Compose 中删除 `env_file`。

在 1Panel 终端执行：

```bash
umask 077
printf 'SOURCE2_1001_APP_SECRET=%s\n' \
  "$(openssl rand -base64 48 | tr -d '\n')" \
  > /opt/zhuojian/secrets/cowagent.env
printf 'SOURCE2_1001_ADMIN_APP_SECRET=%s\n' \
  "$(openssl rand -base64 48 | tr -d '\n')" \
  >> /opt/zhuojian/secrets/cowagent.env
printf 'SOURCE2_1001_DATA_KEY_V1=%s\n' \
  "$(openssl rand -base64 32 | tr -d '\n')" \
  >> /opt/zhuojian/secrets/cowagent.env
chmod 600 /opt/zhuojian/secrets/cowagent.env
```

三项密钥必须不同。将业务 App Secret 安全录入 source2 租户连接配置；管理员 Secret 只用于紧急清理和归档
重加密工具，不提供给日常业务客户端。

环境文件是最低可用方案。生产环境优先由 KMS/密钥管理服务在容器启动时注入。拥有 Docker 管理权限的账号
通常可以检查容器环境变量，因此 Docker/1Panel 管理权限本身也必须视为密钥权限。

## 8. 修正配置文件属主

镜像入口最终会切换为容器内 `agent` 用户。不同基础镜像生成的 UID/GID 可能不同，先由 1Panel 生成一个
一次性检查编排，不在终端直接运行 Docker：

1. 进入“容器 → 编排 → 创建编排 → 编辑”，名称填写 `cowagent-uid-check`。
2. 粘贴以下内容并创建编排：

```yaml
services:
  uid-check:
    image: zhuojian/cowagent:20260919-abcdef
    entrypoint: ["/bin/sh", "-c"]
    command: ["id -u agent; id -g agent"]
    restart: "no"
```

3. 在编排详情打开 `uid-check` 容器日志。第一行是 UID，第二行是 GID；记入本次发布记录。
4. 在 1Panel 中删除 `cowagent-uid-check` 编排。它没有数据卷和业务配置，不能与正式服务同时长期保留。

假设日志两行分别为 `999` 和 `999`，在 1Panel“终端”执行：

```bash
chown 999:999 /opt/zhuojian/cowagent/config.json
chown -R 999:999 /opt/zhuojian/cowagent/data
chmod 600 /opt/zhuojian/cowagent/config.json
chmod 700 /opt/zhuojian/cowagent/data
```

必须替换成日志中的真实数值。每次基础镜像变化都重新检查，不要假设一直是 `999:999`，也不要把包含模型
Key 的配置改成全局可读。

## 9. 由 1Panel 生成并管理 Compose

### 9.1 创建编排模板

进入“容器 → 编排模板 → 创建”，名称填写 `zhuojian-cowagent`，粘贴以下模板。模板只保存配置文本，不会
立即创建容器；密码和私钥继续放在受控的 `env_file` 中，不写入模板。

```yaml
services:
  cowagent:
    image: zhuojian/cowagent:20260919-abcdef
    container_name: zhuojian-cowagent
    restart: unless-stopped
    env_file:
      - /opt/zhuojian/secrets/cowagent.env
    ports:
      - "127.0.0.1:19899:9899"
    volumes:
      - type: bind
        source: /opt/zhuojian/cowagent/config.json
        target: /app/config.json
        read_only: true
        bind:
          create_host_path: false
          selinux: Z
      - type: bind
        source: /opt/zhuojian/cowagent/data
        target: /home/agent/cow
        bind:
          create_host_path: false
          selinux: Z
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - NET_RAW
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9899/', timeout=5)"
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 40s
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
```

只部署普通 Web Agent 且没有 `cowagent.env` 时，删除整个 `env_file` 段。保存模板后，进入“容器 → 编排 →
创建编排”，来源选择“编排模板”，选择 `zhuojian-cowagent`，将编排名称也设为 `zhuojian-cowagent`，检查
镜像标签和宿主机目录后提交。这样生成的编排来源为 `1Panel`，后续才可以直接在面板中编辑、启停和重建。

1Panel 官方说明：模板本身不会创建容器；只有从“编排”创建后才会部署，且只有来源为 `1Panel` 的编排支持
完整启停。参见 [编排模板](https://1panel.cn/docs/v2/user_manual/containers/compose_template/) 和
[容器编排](https://1panel.cn/docs/v2/user_manual/containers/compose/)。

为什么不使用 `cap_drop: ALL`：镜像入口在以 root 启动时需要先修正数据卷根目录属主，再切换到 `agent`
用户；删除全部 capability 可能导致该降权过程失败。这里仅删除医疗部署不需要的 `NET_RAW`。

`selinux: Z` 会把这两个明确的 CowAgent 路径标记为仅供该容器使用。不要把 `/opt`、`/home` 或其他系统级
目录整体加 `Z`；若同一目录确实需要被多个容器共享，应先完成安全评审再改为小写 `z`。

在“容器 → 编排 → `zhuojian-cowagent` → 详情”确认服务已经启动，再到“容器”列表确认
`zhuojian-cowagent` 为健康状态，并从容器操作菜单打开“日志”检查最近 200 行。最后在 1Panel“终端”执行
`curl -I http://127.0.0.1:19899/` 验证根路径返回到 `/chat` 的跳转，再执行
`curl -I http://127.0.0.1:19899/chat` 确认内置前端页面可访问。

合格标准：

- 容器持续运行且健康检查通过；
- 9899 只显示在 `127.0.0.1:19899`，不是 `0.0.0.0`；
- `/chat` 返回完整 CowAgent Web 控制台，而不是 `CowAgent API is running` 诊断占位页；
- 日志没有配置解析、权限或工作区隔离错误；
- 日志没有模型 API Key、HMAC Secret、数据密钥、患者原文或医疗内容。

## 10. 配置 1Panel 反向代理与 HTTPS

### 10.1 创建网站

进入“网站 → 创建网站 → 反向代理”：

| 配置 | 值 |
| --- | --- |
| 主域名 | `ai.example.com` |
| 代理地址 | `http://127.0.0.1:19899` |
| HTTPS | 开启 |
| HTTP | 自动跳转 HTTPS |
| 代理缓存 | 关闭 |

如果 1Panel 所在版本的 OpenResty 无法访问宿主机回环映射，先进入 OpenResty 容器验证目标连通性，再选择
以下一种方式调整：

1. 将 CowAgent 和 OpenResty 加入同一受控 Docker 网络，代理到 `http://zhuojian-cowagent:9899`；
2. 保持回环映射，按本机网络配置使用 OpenResty 可访问的宿主机地址，并用 Docker 端口防护只允许
   OpenResty 来源。

不要为了省事把 `19899` 发布到 `0.0.0.0`。

证书、HTTPS 跳转、HSTS、反向代理和真实 IP 设置可在网站配置中管理，参见
[1Panel 网站配置](https://1panel.cn/docs/v2/user_manual/websites/website_config_basic/)。

### 10.2 普通 Web 控制台模式

这是默认部署方式。CowAgent 后端镜像内置完整 Web 前端，OpenResty 代理整个站点后，浏览器访问根路径会跳转
到 `/chat`；不需要上传或部署单独的 CowAgent 前端 ZIP。对公网开放控制台时必须同时满足：

- CowAgent 设置高强度 `web_password`；
- 1Panel 为站点或根路径增加第二层密码访问或固定办公 IP 白名单；
- 禁止代理缓存；
- HTTPS 强制跳转；
- 管理员退出后不共享浏览器会话。

高级代理配置至少保留：

```nginx
location / {
    proxy_pass http://127.0.0.1:19899;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
    proxy_buffering off;
}
```

### 10.3 Source2 医疗 API 专用模式

生产医疗集成推荐只公开 Source2 API，控制台根路径返回 404。管理员需要操作控制台时，通过 SSH 隧道临时
访问 `127.0.0.1:19899`。

```nginx
location ^~ /api/integrations/source2/v1/ {
    client_max_body_size 50m;
    proxy_pass http://127.0.0.1:19899;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Accept-Encoding identity;
    proxy_request_buffering on;
    proxy_buffering off;
    proxy_connect_timeout 10s;
    proxy_read_timeout 180s;
    gzip off;
}

location / {
    return 404;
}
```

Source2 HMAC 签名包含原始方法、路径和请求正文。代理不得：

- 增加或删除 API 路径前缀；
- 修改 JSON 正文；
- 解压或重新压缩请求；
- 用统一 HTML 登录页替换 JSON 错误；
- 缓存医疗接口响应。

Source2 中的 CowAgent 地址填写 `https://ai.example.com`，不能包含额外路径、查询参数、用户凭据或尾斜杠。

## 11. Source2 医疗接口验收

不接 Source2 时跳过本节。

### 11.1 健康探针

从能够访问正式域名的受控终端运行：

```bash
read -rsp 'Source2 App Secret: ' SOURCE2_PREFLIGHT_SECRET
echo
export SOURCE2_PREFLIGHT_SECRET

python scripts/source2_medical_preflight.py \
  --base-url https://ai.example.com \
  --tenant-id 1001 \
  --agent-id source2-tenant-1001 \
  --app-key source2-1001 \
  --secret-env SOURCE2_PREFLIGHT_SECRET \
  --expected-model-profile medical-commercial-prod \
  --expected-corpus-version 'REPLACE_WITH_PUBLISHED_VERSION' \
  --expected-compliance-ref PRIVACY-2026-001 \
  --expected-encryption-key-id kms/source2/1001/v1 \
  --expected-media-host api.example.com

unset SOURCE2_PREFLIGHT_SECRET
```

命令必须返回 `"preflight": "PASS"` 且退出码为 0。失败时只根据稳定错误码排查，不把请求正文或 Secret
复制到工单。

### 11.2 存储权限探针

首版语料同步并初始化存储后执行：

在 1Panel“容器 → `zhuojian-cowagent` → 终端”中执行：

```bash
python /app/scripts/source2_medical_storage_preflight.py \
  --workspace /home/agent/cow/tenants/1001
```

每个租户增加一个 `--workspace`。探针必须返回 `"storagePreflight": "PASS"`，并确认工作区互不重叠、
没有符号链接逃逸、目录为 `0700`、敏感文件为 `0600`。

### 11.3 业务验收

1. source2 连接测试成功；
2. FAQ/文档发布和同步成功，版本号与健康探针一致；
3. 营业时间、地址、挂号、费用、医保流程等服务问题可以回复；
4. 症状、诊断、治疗、用药、急症、不良反应和低置信度问题全部转人工；
5. 重复请求返回同一幂等结果，不新增重复消息；
6. 租户 A 不能访问租户 B 的 Agent、语料、归档或密钥；
7. 日志和 1Panel 监控中没有患者原文及医疗上下文。

## 12. 防火墙与安全检查

最终端口应为：

| 端口 | 监听范围 | 用途 |
| --- | --- | --- |
| 80 | 公网 | 只负责跳转 HTTPS |
| 443 | 公网 | OpenResty HTTPS |
| 19899 | `127.0.0.1` | OpenResty 到 CowAgent |
| 9899 | 容器内部 | CowAgent Web 服务 |

只读检查：

```bash
ss -lntup
```

同时在 1Panel“容器”列表和容器详情的端口页确认只显示 `127.0.0.1:19899 → 9899`，不出现
`0.0.0.0:19899` 或公网地址绑定。

还应确认：

- 1Panel 面板和 SSH 只允许运维 IP；
- CentOS Stream 10 的 SELinux 保持 `Enforcing`，firewalld 保持运行；
- 云安全组不开放 19899/9899；
- Docker 端口防护没有遗留的“允许所有来源”；
- `config.json` 不可被普通系统用户读取；
- 模型供应商已经签署数据保护协议并关闭训练；
- 医疗永久留存已有书面审批；
- Web 控制台没有开启 Shell、普通网络搜索、自我进化等医疗链路无关能力。

## 13. 备份

至少备份：

1. `/opt/zhuojian/cowagent/data`；
2. `/opt/zhuojian/cowagent/config.json`；
3. Source2 HMAC、管理员和历史/活动数据密钥；
4. 当前及上一版镜像标签、镜像 ID、导出 `.tar` 和 SHA-256；
5. 1Panel 网站/OpenResty 配置及证书。

要求：

- 备份文件加密；
- 备份账号与运行账号分离；
- 保留历史数据密钥，否则旧归档不可解密；
- 定期执行恢复演练；
- 不把 `run.log` 当成审计备份；
- 医疗归档永久保留时，容量监控必须覆盖备份增长量。

1Panel 支持网站配置备份，但它不能替代 CowAgent 数据目录和密钥的独立备份。网站备份说明见
[1Panel 网站备份](https://1panel.cn/docs/v2/user_manual/websites/website_backup/)。

## 14. 升级与回滚

### 14.1 升级

1. 上传已通过门禁的新发布源码，并在 1Panel“容器 → 镜像”构建新的固定标签镜像；
2. 备份配置、工作区和全部密钥；
3. 从 1Panel 导出新旧镜像，记录镜像 ID 和归档 SHA-256；
4. 在 1Panel 编排中把 `image` 改为新标签；
5. 由 1Panel 重新创建编排服务，不删除数据目录；
6. 检查日志、容器健康、HTTPS 和 Source2 健康探针；
7. 单租户观测通过后再恢复自动回复。

### 14.2 回滚

1. source2 先关闭目标租户自动回复或开启观测模式；
2. 1Panel 编排恢复上一镜像标签/摘要；
3. 重建容器，但保留 `config.json`、工作区和密钥；
4. 重新执行健康和存储探针；
5. 确认旧版本理解当前数据格式后再恢复业务。

不要通过删除 `/opt/zhuojian/cowagent/data` 解决启动问题；该目录可能包含需要永久保留的医疗归档。

## 15. 常见故障

| 现象 | 原因与处理 |
| --- | --- |
| 容器内没有 Source2 API | 使用了上游镜像或仓库根目录旧 `Dockerfile`；按第 4 节在源码根目录创建 `Dockerfile.1panel` 并由 1Panel 构建 |
| 1Panel 构建时找不到 `requirements.txt` | 直接选择了 `docker/Dockerfile.latest`，导致构建上下文只有 `docker/`；改选源码根目录的 `Dockerfile.1panel` |
| Bullseye 或 `debian-security` 下载返回 404 | 使用了旧部署包中的 Debian 11 镜像；改用 Bookworm 修复版后端 ZIP。若仍有镜像同步问题，先把 `USE_CN_MIRROR` 改为 `false` 并关闭构建缓存重试 |
| `config.json` Permission denied | 宿主文件是 `root:root 0600`；按镜像实际 UID/GID 重新 `chown` |
| CentOS 日志出现 `avc: denied` | 确认使用 Compose 长语法且两个 bind mount 都设置 `bind.selinux: Z`；不要关闭 SELinux |
| `exec /entrypoint.sh: no such file or directory` | Windows 生成的旧部署包把脚本保存成 CRLF，Linux 实际找的是 `/bin/bash\r`；使用修复版后端 ZIP，以新标签关闭缓存重建镜像，再更新正式编排 |
| 页面只显示 `CowAgent API is running` | 使用了把 Web 页面替换为诊断占位页的旧后端 ZIP；上传新版后端 ZIP，以新标签关闭缓存重建镜像，OpenResty 整站代理到 `127.0.0.1:19899` |
| 容器不断重启 | 检查 JSON 格式、模型配置、挂载路径和入口降权错误 |
| 浏览器无法访问 | 检查 127.0.0.1 映射、OpenResty 到宿主机连通性、网站状态和证书 |
| 502 Bad Gateway | CowAgent 未监听 9899、反代目标错误、OpenResty 无法访问回环映射 |
| Source2 返回 401/403 | App Key/Secret、tenantId、Agent ID、服务器时间或防重放失败 |
| 语料同步返回 413 | OpenResty `client_max_body_size` 小于 50 MiB |
| `STORAGE_PATH_NOT_ISOLATED` | 租户工作区重叠、符号链接、挂载路径逃逸 |
| `DATA_KEY_NOT_ISOLATED` | 不同租户复用了同一数据密钥，必须生成独立密钥 |
| 响应编码或媒体类型错误 | 代理压缩、缓存、WAF HTML 错误页或响应变换 |
| 配置改了但未生效 | `config.json` 是只读部署；修改宿主文件后重启容器并核对实际挂载 |

## 16. 个人微信说明

若同时使用 Bubblebot 个人微信能力，微信操作端仍部署在本地 Windows 电脑，不放在 1Panel 的 CowAgent
容器中。云端 CowAgent 保持 `channel_type=web`；本地微信端通过主动 HTTPS/WSS 连接云端，不向公网开放本地
自动化端口，也不把微信登录态、Cookie 或二维码上传到云服务器。
