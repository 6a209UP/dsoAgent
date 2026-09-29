# 卓见口腔智能客服系统 1Panel 部署教程

> 更新时间：2026-09-19  
> 适用范围：source2 诊疗卡后台、管理端、患者 H5/小程序、CowAgent 医患智能客服  
> 1Panel 基线：V2 版本界面  
> 宿主机基线：CentOS Stream release 10 (Coughlan)  
> 主文档：`D:\workspace\AI\CowAgent\docs\zh\development\1panel-deployment-guide.md`  
> source2 入口索引：`D:\workspace\Medical Card\source2\1panel-deployment-guide.md`

## 1. 部署结论

本教程推荐使用下列生产拓扑：

```text
互联网
  │
  ├─ admin.example.com ── 1Panel/OpenResty ── 管理端静态文件
  ├─ h5.example.com    ── 1Panel/OpenResty ── 患者 H5 静态文件
  ├─ api.example.com   ── 1Panel/OpenResty ── source2 Gateway:48080
  └─ ai.example.com    ── 1Panel/OpenResty ── CowAgent:9899
                                                    │
source2：Gateway ── Nacos ── system/infra/member/pay/medical-server
                              │
                              ├─ MySQL 8.0+
                              └─ Redis

CowAgent：每个 tenantId ── 独立 Agent ── 独立工作区/语料/密钥
```

主线按“已有 source2 微服务环境增量部署”编写。仓库里的网关已经配置
`/admin-api/medical/**` 和 `/app-api/medical/**` 到 `medical-server`，医疗服务默认端口为
`49082`，网关默认端口为 `48080`。

全新服务器仍需先部署 source2 的基础服务（Nacos、system、infra、member、pay、gateway），
再按本教程部署 medical-server 与 CowAgent。不要把当前 `yudao-server.jar` 当成完整诊疗卡单体包：
当前 `yudao-server/pom.xml` 未引入 `yudao-module-medical-server`，现有构建产物也不包含医疗模块。
若未来改为单体部署，必须先完成模块装配并验证 JAR 内确实含医疗模块。

1Panel V2 可以从“容器 → 编排”通过在线编辑、服务器路径或编排模板创建 Compose；由
1Panel 创建的编排才支持在面板中完整启停。官方说明见
[1Panel 容器编排](https://1panel.cn/docs/v2/user_manual/containers/compose/)。CowAgent 镜像也由
“容器 → 镜像 → 构建镜像”从发布源码生成，不在终端执行 Docker 构建、推送或拉取，参见
[1Panel 镜像](https://1panel.cn/docs/v2/user_manual/containers/image/)。

## 2. 当前仓库的上线阻断检查

上线前必须处理以下项目：

1. **CowAgent 必须构建本仓库源码镜像。** 根目录原有 `Dockerfile` 只是继承上游镜像，不会复制本次
   Source2 医疗集成代码。按第 4.5 节把 `docker/Dockerfile.latest` 复制为发布源码根目录的
   `Dockerfile.1panel`，再由 1Panel 生成固定版本镜像。
2. **不要使用当前 `yudao-server.jar` 承载诊疗卡。** 当前单体 JAR 不含 medical 模块；本教程使用
   `yudao-module-medical-server.jar` 加现有 Gateway/Nacos 微服务链路。
3. **清除并轮换仓库中的示例或历史凭证。** 后端 YAML 中存在开发地址、默认密码和形似 API Key 的
   示例值。生产环境必须通过 Nacos、环境变量或密钥服务覆盖，并对曾经真实使用过的值执行轮换。
4. **修改前端旧地址。** 管理端 `.env.prod` 与 uni-app `.env` 仍包含旧 IP/HTTP 地址，构建前必须替换
   为正式 HTTPS 域名。
5. **不使用 `latest`。** source2 和 CowAgent 镜像都使用 Git 提交号或发布号作为不可变标签，并保留上一版。
6. **先通过发布门禁。** 测试、数据库迁移计划和前端构建未全部成功时，不上传生产制品。
7. **模型 API Key 不要直接用现有环境变量覆盖机制注入。** 当前 CowAgent `config.py` 会在 INFO 日志中
   输出受支持环境变量的覆盖值；在完成该日志脱敏修复前，应通过权限为 `0600` 的只读 `config.json`
   注入模型 Key，并逐行检查启动日志。Source2 HMAC/数据密钥使用的专用环境变量不属于这组通用配置项。

可以用下面的只读方法验证 source2 JAR 是否包含医疗模块。结果必须至少看到
`BOOT-INF/lib/yudao-module-medical-server-*.jar`；当前仓库的单体 JAR会返回空结果。

```bash
unzip -l yudao-server.jar | grep 'BOOT-INF/lib/.*medical'
```

## 3. 服务器、域名与端口准备

### 3.1 建议资源

| 场景 | 建议起点 | 说明 |
| --- | --- | --- |
| 已有 source2，仅增加 CowAgent/medical-server | 4 核、8 GiB、100 GiB SSD | 模型使用外部商业 API |
| 全套 source2 微服务同机 | 8 核、16 GiB、200 GiB SSD | Nacos、多个 JVM、MySQL、Redis 同机 |
| 较多租户或媒体长期归档 | 单独数据盘并持续扩容 | CowAgent 医疗归档按方案永久保留 |

生产环境至少准备四个域名：

| 域名示例 | 用途 | 公网端口 |
| --- | --- | --- |
| `admin.example.com` | source2 管理端 | 443 |
| `h5.example.com` | 患者 H5 | 443 |
| `api.example.com` | source2 API 与 WebSocket | 443 |
| `ai.example.com` | Source2 调用 CowAgent 的专用 API | 443 |

公网只开放 80/443；SSH 和 1Panel 面板只允许固定运维 IP。MySQL、Redis、Nacos、48080、49082、
9899 不直接暴露公网。Docker 发布端口可能走独立转发链路，不能只检查系统防火墙；还要检查
1Panel“Docker 端口防护”和云安全组，参见
[1Panel 防火墙文档](https://1panel.cn/docs/v2/user_manual/hosts/firewall/)。

CentOS Stream 10 保持 SELinux `Enforcing` 和 firewalld 启用，不使用 `setenforce 0` 或停防火墙解决部署
问题。1Panel 统一使用 Docker 管理容器，不要再用 Podman 或 `podman-docker` 操作同一编排。CowAgent 的
宿主机 bind mount 必须使用下面 Compose 中的私有 SELinux `Z` 标签。

### 3.2 1Panel 基础组件

在“应用商店”安装：

- OpenResty；
- MySQL 8.0 或兼容版本；
- Redis；
- 已有微服务环境没有 Nacos 时，再安装 Nacos。

应用安装的高级设置可以控制外部端口和资源限制，数据库与 Redis 均关闭“端口外部访问”。操作方式可参考
[应用安装](https://1panel.cn/docs/v2/user_manual/appstore/install/)、
[MySQL](https://1panel.cn/docs/v2/user_manual/appstore/mysql/)、
[Redis](https://1panel.cn/docs/v2/user_manual/appstore/redis/) 和
[OpenResty](https://1panel.cn/docs/v2/user_manual/appstore/openresty/)。

在 MySQL 中创建独立数据库和最小权限用户，例如数据库 `source2`，字符集使用 `utf8mb4`。Redis 设置高强度
密码。应用容器需要连接数据库时，优先把相关容器加入同一个受控 Docker 网络并使用连接信息中显示的服务名；
若通过宿主机映射端口连接，则只绑定内网/本机并限制来源，不要授权数据库用户给任意公网地址。

## 4. 发布前构建

### 4.1 运行统一发布门禁

在 Windows 构建机的 CowAgent 仓库执行：

```powershell
pwsh -NoProfile -File .\scripts\source2_medical_release_check.ps1 `
  -Source2Root 'D:\workspace\Medical Card\source2' `
  -JavaHome 'D:\workspace\IOT\jdk-21.0.6'
```

正式发布不能使用 `-SkipFrontendBuild`。该门禁会验证 CowAgent 协议与安全测试、跨项目契约、数据库迁移
计划、source2 医疗后端测试、管理端定向类型检查和生产构建。

### 4.2 构建 source2 医疗服务与网关

在 `D:\workspace\Medical Card\source2\yudao-cloud` 执行：

```powershell
mvn -pl yudao-module-medical/yudao-module-medical-server -am clean package -DskipTests
mvn -pl yudao-gateway -am package -DskipTests
```

发布制品：

- `yudao-module-medical/yudao-module-medical-server/target/yudao-module-medical-server.jar`
- `yudao-gateway/target/yudao-gateway.jar`（仅当生产网关版本尚未包含 medical 路由时升级）

计算并保存 SHA-256：

```powershell
Get-FileHash -Algorithm SHA256 `
  .\yudao-module-medical\yudao-module-medical-server\target\yudao-module-medical-server.jar
Get-FileHash -Algorithm SHA256 .\yudao-gateway\target\yudao-gateway.jar
```

### 4.3 构建管理端

先修改 `D:\workspace\Medical Card\source2\yudao-ui-admin-vue3\.env.prod`：

```dotenv
VITE_BASE_URL='https://api.example.com'
VITE_API_URL=/admin-api
VITE_BASE_PATH=/
VITE_OUT_DIR=dist-prod
```

然后构建：

```powershell
cd 'D:\workspace\Medical Card\source2\yudao-ui-admin-vue3'
pnpm install --frozen-lockfile
pnpm ts:check:medical-ai
pnpm build:prod
```

上传目录为 `dist-prod`，不是项目源码目录。

### 4.4 构建患者 H5/小程序

修改 `D:\workspace\Medical Card\source2\yudao-mall-uniapp\.env`：

```dotenv
SHOPRO_BASE_URL=https://api.example.com
SHOPRO_API_PATH=/app-api
SHOPRO_WEBSOCKET_PATH=/infra/ws
SHOPRO_STATIC_URL=https://h5.example.com
SHOPRO_H5_URL=https://h5.example.com
SHOPRO_TENANT_ID=1
```

该仓库没有可直接发布 H5 的 npm build 命令，使用 HBuilderX 的“发行 → 网站-H5”生成
`unpackage/dist/build/h5`；微信小程序使用“发行 → 小程序-微信”，再在微信开发者工具上传审核。
正式小程序还需在微信公众平台配置 `https://api.example.com` 的 request/upload/download 合法域名，
并配置 `wss://api.example.com` 的 socket 合法域名。

### 4.5 构建 CowAgent 自定义镜像

本教程由 1Panel 在目标服务器本地生成镜像，不使用命令行 `docker build/push/pull`：

1. 将已通过发布门禁的干净源码包上传并解压到
   `/opt/zhuojian/build/cowagent-20260919-abcdef`。源码包不得包含 `.git`、真实配置、密钥、日志或工作区。
2. 在 1Panel“主机 → 文件”中，把 `docker/Dockerfile.latest` 的完整内容复制到源码根目录新文件
   `Dockerfile.1panel`。
3. 进入“容器 → 镜像 → 构建镜像”，名称填写 `zhuojian/cowagent:20260919-abcdef`，Dockerfile 来源选
   “路径选择”，选择源码根目录的 `Dockerfile.1panel`。
4. 构建参数每行填写一个：`INSTALL_BROWSER=false`、`USE_CN_MIRROR=true`。境外服务器或受控软件源环境把
   `USE_CN_MIRROR` 改为 `false`。
5. 构建成功后在镜像列表记录镜像 ID、创建时间和大小，并用 1Panel“导出”保存 `.tar` 归档及 SHA-256。

1Panel 会把所选 Dockerfile 所在目录作为构建上下文，因此不能直接选择 `docker/Dockerfile.latest`；否则
上下文只有 `docker/` 子目录，无法加入完整源码。医疗接口不需要 Playwright 浏览器，关闭它可以减小镜像并
减少无关工具面。当前部署镜像基于 Debian 12 Bookworm；不要继续使用已结束 LTS 的 Bullseye 部署包。
建议使用 1Panel `2.0.13` 或更高版本；如果“构建镜像”没有构建参数输入框，先升级面板。

更完整的上传、构建日志、镜像导出和回滚步骤见
[CowAgent 在 1Panel 中的部署教程](./cowagent-1panel-deployment-guide.md)。

## 5. 数据库初始化或升级

### 5.1 已有业务库

先在 1Panel“数据库 → MySQL → 备份”创建带压缩密码的备份，并在隔离环境完成一次恢复演练。1Panel V2
支持数据库备份、恢复和计划任务定时备份，参见
[1Panel MySQL 管理](https://1panel.cn/docs/v2/user_manual/appstore/mysql/)。

从受控运维机的 `yudao-cloud` 目录执行：

```powershell
# 只检查 11 个升级文件及顺序，不连接数据库
pwsh -NoProfile -File .\script\db\run-medical-backend-upgrade.ps1 -PlanOnly

# 脚本自身的安全与顺序测试
pwsh -NoProfile -File .\script\db\test-medical-backend-upgrade-runner.ps1
pwsh -NoProfile -File .\script\db\test-medical-cowagent-migration-runner.ps1

# 完成可恢复备份后，在维护窗口执行
pwsh -NoProfile -File .\script\db\run-medical-backend-upgrade.ps1 `
  -Apply -BackupConfirmed `
  -MySqlPath 'C:\secure-tools\mysql.exe' `
  -DefaultsExtraFile 'C:\secure\source2-db-client.cnf' `
  -Database 'source2'
```

远程生产数据库的 `source2-db-client.cnf` 必须使用受限权限，并包含：

```ini
[client]
host=db.internal.example.com
port=3306
user=source2_migrator
password=从密钥系统临时取得
ssl-mode=VERIFY_IDENTITY
ssl-ca=C:/secure/ca/mysql-ca.pem
```

不要在命令行、Compose、工单或聊天中传递数据库密码。只有脚本和 MySQL 都在同一可信主机、host 为
`localhost`、`127.0.0.1` 或 `::1` 时，才可显式使用 `-AllowInsecureLocalDatabase`。

### 5.2 全新空库

空库使用 source2 根目录 `诊疗卡系统数据库发布安装.sql`，或依次执行：

1. `sql/mysql/ruoyi-vue-pro.sql`
2. `sql/mysql/quartz.sql`
3. `sql/mysql/medical-card.sql`
4. 随后仍执行 `run-medical-backend-upgrade.ps1` 补齐后续增量

全量安装脚本包含 `DROP TABLE IF EXISTS`，严禁用于已有业务数据的数据库。不要再执行旧版
`medical.sql`，也不要额外执行会物理删除并重建菜单的 `medical-menu-rebuild.sql`。

## 6. 在 1Panel 部署 source2

### 6.1 保留现有微服务拓扑

已有 source2 环境只升级 medical-server；生产网关不含医疗路由时再升级 gateway。不要在同一次上线中顺便
把微服务改成单体。

在 1Panel“网站 → 运行环境 → Java”创建 Java 21 运行环境，上传医疗 JAR，并设置：

- 应用名：`zhuojian-medical-server`
- 启动命令：`java -Xms512m -Xmx1536m -jar /app/yudao-module-medical-server.jar`
- 应用端口：`49082`
- 对外端口：关闭
- 自动重启：开启
- 时区：`Asia/Shanghai`

将该运行环境接入与 Gateway、Nacos、MySQL、Redis 可互通的受控 Docker 网络；如果面板必须发布宿主机
端口，只绑定 `127.0.0.1` 或使用 Docker 端口防护拒绝全部公网来源。服务注册后的容器地址必须能够被
Gateway 访问，不能把仅在 medical-server 容器内成立的 `127.0.0.1` 注册到 Nacos。

1Panel 当前支持 Java 8、11、17、18、21、22 等运行环境，创建后可以在列表中启停、重启和查看日志，
参见 [1Panel Java 运行环境](https://1panel.cn/docs/v2/user_manual/websites/java/)。

生产环境设置 `SPRING_PROFILES_ACTIVE=prod`，并至少通过环境变量或 Nacos 的
`medical-server-prod.yaml` 注入以下配置：

下面的 `*.internal`、命名空间和 `REPLACE_*` 都是占位值，必须替换为 1Panel“连接信息”或密钥系统中的
实际值：

```dotenv
SPRING_CLOUD_NACOS_SERVER_ADDR=nacos.internal:8848
SPRING_CLOUD_NACOS_CONFIG_SERVER_ADDR=nacos.internal:8848
SPRING_CLOUD_NACOS_DISCOVERY_SERVER_ADDR=nacos.internal:8848
SPRING_CLOUD_NACOS_DISCOVERY_NAMESPACE=prod-namespace-id
SPRING_CLOUD_NACOS_CONFIG_NAMESPACE=prod-namespace-id
SPRING_CLOUD_NACOS_USERNAME=REPLACE_FROM_SECRET_STORE
SPRING_CLOUD_NACOS_PASSWORD=REPLACE_FROM_SECRET_STORE

SPRING_DATASOURCE_DYNAMIC_DATASOURCE_MASTER_URL=jdbc:mysql://mysql.internal:3306/source2?sslMode=VERIFY_IDENTITY&serverTimezone=Asia/Shanghai&characterEncoding=UTF-8&rewriteBatchedStatements=true
SPRING_DATASOURCE_DYNAMIC_DATASOURCE_MASTER_USERNAME=source2_app
SPRING_DATASOURCE_DYNAMIC_DATASOURCE_MASTER_PASSWORD=REPLACE_FROM_SECRET_STORE
SPRING_REDIS_HOST=redis.internal
SPRING_REDIS_PORT=6379
SPRING_REDIS_PASSWORD=REPLACE_FROM_SECRET_STORE

MEDICAL_AI_CONFIG_ENCRYPTION_KEY=REPLACE_WITH_EXACTLY_32_ASCII_CHARS
MEDICAL_AI_MEDIA_PROXY_BASE_URL=https://api.example.com/app-api/medical/ai/media
MEDICAL_AI_LOCAL_WORKER_ENABLED=true
MEDICAL_AI_WORKER_DELAY_MS=2000
MEDICAL_AI_LOCAL_WORKER_MAX_TASKS_PER_TENANT=1
```

`MEDICAL_AI_CONFIG_ENCRYPTION_KEY` 可以用 `openssl rand -hex 16` 生成。它用于解密数据库中保存的
CowAgent App Secret，丢失或随意更换会导致已有连接配置无法读取，因此必须进入密钥托管和备份流程。
MySQL 的生产 JDBC 连接还要挂载受信任 CA/TrustStore；不能完成服务端身份校验时，不要把
`VERIFY_IDENTITY` 降为明文连接来绕过上线检查。

还要外部化 JWT/API 加密、微信、小程序、对象存储、支付和邮件等生产凭证；不得沿用仓库 YAML 的开发值。
若使用 XXL-Job 触发 AI 任务，把 `MEDICAL_AI_LOCAL_WORKER_ENABLED` 改为 `false`，避免两套 Worker 同时运行。

### 6.2 网关

确认生产网关版本包含：

```yaml
- id: medical-admin-api
  uri: grayLb://medical-server
  predicates:
    - Path=/admin-api/medical/**
- id: medical-app-api
  uri: grayLb://medical-server
  predicates:
    - Path=/app-api/medical/**
```

网关只绑定宿主机或 Docker 内网的 `48080`，由 OpenResty 代理。启动后在 Nacos 检查
`gateway-server`、`medical-server`、`system-server`、`infra-server`、`member-server` 和 `pay-server`
均有健康实例。

## 7. 在 1Panel 部署 CowAgent

本节的独立、可直接执行版本见
[CowAgent 在 1Panel 中的部署教程](cowagent-1panel-deployment-guide.md)。如果两份文档存在差异，CowAgent
容器、配置、权限和反向代理操作以独立教程为准。

### 7.1 创建目录和密钥

在 1Panel 终端执行，实际目录可以调整，但必须使用绝对路径：

```bash
install -d -m 700 /opt/zhuojian/cowagent
install -d -m 700 /opt/zhuojian/cowagent/data
install -d -m 700 /opt/zhuojian/secrets
umask 077
openssl rand -base64 48 > /opt/zhuojian/secrets/source2-1001-app-secret
openssl rand -base64 48 > /opt/zhuojian/secrets/source2-1001-admin-secret
openssl rand -base64 32 > /opt/zhuojian/secrets/source2-1001-data-key-v1
```

业务 App Secret、归档管理员 Secret、医疗数据密钥必须互不相同；不同租户也不得复用。将值注入专用
环境变量，不要写进 `config.json`。示例 `/opt/zhuojian/secrets/cowagent.env`：

```dotenv
SOURCE2_1001_APP_SECRET=<source2-1001-app-secret 文件内容>
SOURCE2_1001_ADMIN_APP_SECRET=<source2-1001-admin-secret 文件内容>
SOURCE2_1001_DATA_KEY_V1=<source2-1001-data-key-v1 文件内容>
```

设置文件权限为 `0600`。正式环境优先由 KMS/密钥管理系统在启动时注入，而不是长期保存在普通磁盘文件。

### 7.2 创建 CowAgent 配置

创建 `/opt/zhuojian/cowagent/config.json`。下面只展示必要结构，模型名称、API 地址、租户和审批号必须
替换为生产批准值：

```json
{
  "cow_lang": "zh",
  "channel_type": "web",
  "web_host": "0.0.0.0",
  "web_port": 9899,
  "web_password": "替换为独立高强度控制台密码",
  "agent": true,
  "self_evolution_enabled": false,
  "open_ai_api_base": "https://approved-model.example.com/v1",
  "open_ai_api_key": "通过受控只读配置或密钥挂载注入",
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

保存后按独立 CowAgent 教程创建一次性 `cowagent-uid-check` 编排，从 1Panel 容器日志读取镜像内 `agent`
用户的实际 UID/GID，再把 `config.json` 的属主改成该值并执行 `chmod 600`。不能直接保留为
`root:root 0600`，否则镜像入口切换用户后无法读取。`cowagent.env` 由容器运行时读取，保持
`root:root 0600`。
上例把模型 API Key 放在只读配置中是针对当前通用环境变量日志行为的临时上线方式；配置文件必须只允许
CowAgent 服务账号读取，后续应改为不会输出值的密钥注入实现。

医疗链路必须关闭自我进化；Source2 专用实现本身不会启用 Shell、普通网络搜索或无关工具。商业模型必须
完成关闭训练、数据保护协议、传输加密、数据驻留和最短供应商留存审查。永久保存医疗上下文仍需隐私与
合规负责人书面批准，审批号写入 `compliance_approval_ref`。

### 7.3 创建 1Panel 编排

先进入“容器 → 编排模板 → 创建”，名称填写 `zhuojian-cowagent`，粘贴：

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
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
```

不要使用仓库 `docker/docker-compose.yml` 的默认生产配置：它引用上游镜像、启用个人微信与自我进化，
且没有 Source2 租户密钥配置。Compose 模板也不应包含密码或私钥；1Panel 官方同样建议模板使用环境变量
文件或 Docker Secret，见 [编排模板](https://1panel.cn/docs/v2/user_manual/containers/compose_template/)。

保存模板后，进入“容器 → 编排 → 创建编排”，来源选择“编排模板”，选择 `zhuojian-cowagent`，检查本地
镜像标签与宿主机路径后创建。这样生成的编排来源为 `1Panel`，可直接在面板中编辑、启停和重建；模板本身
不会自动创建容器。

启动后，在编排详情确认服务已运行，在“容器”列表确认健康状态，并从容器操作菜单查看最近 200 行日志。
最后在 1Panel“终端”执行 `curl -I http://127.0.0.1:19899/` 验证回环地址。

日志中不得出现患者原文、医疗内容、App Secret、数据密钥或模型 API Key。

## 8. 配置 1Panel 网站、HTTPS 与 WebSocket

1Panel 支持静态网站和反向代理网站，并可在创建时直接启用 HTTPS，参见
[创建网站](https://1panel.cn/docs/v2/user_manual/websites/website_create/)；证书、HTTPS 跳转、HSTS、CORS、
真实 IP 和反向代理可在网站配置中维护，参见
[网站配置](https://1panel.cn/docs/v2/user_manual/websites/website_config_basic/)。

### 8.1 管理端与 H5

分别创建两个“静态网站”：

- `admin.example.com`：上传 `dist-prod` 内的文件到站点根目录；
- `h5.example.com`：上传 `unpackage/dist/build/h5` 内的文件到站点根目录。

两个站点都启用 HTTPS、HTTP 自动跳转 HTTPS，并配置 history 路由回退：

```nginx
location / {
    try_files $uri $uri/ /index.html;
}
```

### 8.2 source2 API

创建反向代理网站 `api.example.com`，代理到 `http://127.0.0.1:48080`。开启 HTTPS，不启用代理缓存；
请求体上限至少与后端一致（当前单文件 16 MiB、总请求 32 MiB）。为 `/infra/ws` 启用 WebSocket：

```nginx
location /infra/ws {
    proxy_pass http://127.0.0.1:48080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 300s;
}
```

不要重写 `/admin-api`、`/app-api` 或 `/infra/ws` 的路径。

### 8.3 CowAgent Web 控制台与 Source2 API

CowAgent 后端镜像已经内置完整 Web 前端，不需要另外部署 CowAgent 静态前端。创建反向代理网站
`ai.example.com`，把整个站点代理到 `http://127.0.0.1:19899`；浏览器访问 `/` 后会跳转到 `/chat`，前端
继续通过同域调用后端接口。高级配置示例：

```nginx
location / {
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
    proxy_read_timeout 300s;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_cache off;
    gzip off;
}
```

如果生产环境明确只允许 Source2 调用、不允许公网打开 CowAgent 控制台，才改用独立 CowAgent 教程中的
“Source2 医疗 API 专用模式”，只代理 `/api/integrations/source2/v1/` 并让根路径返回 404。

HMAC 签名包含原始方法、路径和正文，因此不能给 Source2 API 加路径前缀、去除路径段或变换正文。不要开启
响应压缩、缓存、登录页替换或会修改 JSON 的 WAF 规则。证书启用后，source2 中的 CowAgent 地址填写
`https://ai.example.com`，不能带额外路径、查询串或尾部斜杠。

## 9. 配置租户并完成联调

### 9.1 source2 平台配置

平台管理员进入“医疗管理 → 智能客服”，为机构 tenantId `1001` 配置：

| 字段 | 示例 |
| --- | --- |
| 部署模式 | `SHARED` |
| CowAgent 地址 | `https://ai.example.com` |
| Agent ID | `source2-tenant-1001` |
| App Key | `source2-1001` |
| App Secret | 与 CowAgent 环境变量完全一致，至少 32 字节 |
| 模型配置标识 | `medical-commercial-prod` |
| 最低自动回复置信度 | 不低于 70% |
| 连接/响应超时 | 按模型 SLA 设置 |
| 合规审批 | 已批准，并填写书面审批引用 |
| 观测模式 | 首次上线先开启 |

保存后执行“连接测试”。再由租户管理员维护 FAQ/文档、发布不可变版本并确认同步成功。每个租户必须创建
独立 Agent、工作区、App Key/Secret 和数据密钥；不要复制上一租户的整段配置后只改 tenantId。

### 9.2 只读健康探针

在 CowAgent 仓库或镜像内运行：

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
  --expected-corpus-version '<source2 发布后显示的版本>' \
  --expected-compliance-ref PRIVACY-2026-001 \
  --expected-encryption-key-id kms/source2/1001/v1 \
  --expected-media-host api.example.com
unset SOURCE2_PREFLIGHT_SECRET
```

必须返回 `"preflight": "PASS"` 且退出码为 0；密钥只通过环境变量传入，不放在命令行。

首版语料同步并产生医疗存储目录后，在 1Panel“容器 → `zhuojian-cowagent` → 终端”执行文件权限探针：

```bash
python /app/scripts/source2_medical_storage_preflight.py \
  --workspace /home/agent/cow/tenants/1001
```

### 9.3 灰度顺序

1. 只启用一个测试租户，并开启观测模式；此时记录决策但不自动给患者回复。
2. 验证挂号、营业时间、地址、费用、医保流程、材料准备等机构服务问题。
3. 验证诊断、用药、急症、不良反应、低置信度和无语料问题全部 `HANDOFF`。
4. 验证医生抢先回复、重复任务、超时重试和迟到响应不会产生重复消息。
5. 验证图片、语音、附件的一次性 URL 只能消费一次，日志不含签名地址和医疗正文。
6. 观测结果经业务和合规确认后，再关闭观测模式并逐租户启用自动回复。

## 10. 上线验收清单

- [ ] 管理端、H5、API、CowAgent 四个域名证书有效，HTTP 自动跳转 HTTPS。
- [ ] 公网只开放 80/443；MySQL、Redis、Nacos、48080、49082、9899 均不可公网直连。
- [ ] `medical-server` 已注册到生产 Nacos，Gateway 路由返回真实接口而不是 404。
- [ ] 数据库 11 个升级文件完成，五张医疗 AI 表、菜单、通知模板和唯一索引验收通过。
- [ ] `MEDICAL_AI_CONFIG_ENCRYPTION_KEY` 恰好 32 字节且已安全备份。
- [ ] CowAgent 使用自定义固定标签镜像，不是上游 `latest`。
- [ ] 每租户 Agent、工作区、HMAC Secret 和数据密钥相互隔离。
- [ ] CowAgent 健康探针和存储权限探针退出 0。
- [ ] 管理端语料可以创建、发布、同步、失败重试和版本回滚。
- [ ] 服务类问题自动回复；医疗风险和低置信度问题只转人工。
- [ ] AI 回复患者端仍显示医生身份，后台审计为 `service_type=cowagent_ai`。
- [ ] WebSocket 在管理端和患者端都能收到实时消息。
- [ ] 日志、指标、错误页和代理日志中没有患者原文、医疗内容、Secret 或签名 URL。
- [ ] MySQL、CowAgent 工作区、上传文件和配置已有加密备份，并完成恢复演练。

## 11. 备份、升级与回滚

### 11.1 必备备份

- 1Panel MySQL 加密备份；
- source2 上传文件/对象存储；
- `/opt/zhuojian/cowagent/data`；
- CowAgent `config.json` 的受控备份；
- KMS 中的所有活动及历史数据密钥；
- 1Panel 网站/OpenResty 配置和证书；
- 本次 JAR、前端压缩包、镜像 ID、镜像导出归档、数据库迁移输出和 SHA-256。

1Panel 支持网站备份恢复，但不能代替数据库、CowAgent 工作区与外部对象存储的独立备份。网站备份说明见
[1Panel 网站备份](https://1panel.cn/docs/v2/user_manual/websites/website_backup/)。

### 11.2 升级顺序

1. 暂停目标租户自动回复或开启观测模式。
2. 完成数据库备份与恢复验证。
3. 运行数据库迁移。
4. 升级 medical-server，必要时升级 gateway。
5. 在 1Panel 构建新的 CowAgent 固定标签镜像，并编辑编排切换标签后重建服务。
6. 发布管理端/H5 静态文件。
7. 运行健康与存储探针，再执行单租户灰度。

### 11.3 回滚

出现异常时，先在 source2 关闭目标租户 CowAgent 集成；旧关键词自动回复会恢复，医生工作台继续人工处理。
随后把 medical-server、gateway、CowAgent 镜像和静态资源恢复到上一固定版本。数据库 DDL 具有隐式提交，
不要在不确认新增业务数据的情况下直接恢复整库；先依据迁移输出判断已落地步骤。不要删除 CowAgent 工作区，
避免破坏永久医疗归档和审计链。

## 12. 个人微信本地端说明

Bubblebot 的个人微信操作端仍部署在本地 Windows 电脑，不放到 1Panel 云服务器。微信登录态、二维码、Cookie
和自动化运行环境只保存在本地；本地主动通过 HTTPS/WSS 访问云端受控接口，不从公网开放本地端口。该链路
与 source2 医患聊天 API 分开配置，不能把个人微信消息直接写入医疗语料或绕过 source2 的租户、医患关系、
授权和审计校验。

## 13. 常见故障

| 现象 | 优先检查 |
| --- | --- |
| `/admin-api/medical/**` 返回 404 | 网关版本、Nacos 中 `medical-server` 实例、路由配置 |
| medical-server 启动即退出 | 32 字节加密密钥、数据库/Redis/Nacos连接、生产配置是否加载 |
| CowAgent 接口 401/403 | tenantId、Agent ID、App Key/Secret、服务器时间、Nonce、防重放 |
| CowAgent 返回 `STORAGE_PATH_NOT_ISOLATED` | 工作区重叠、符号链接、挂载路径与权限 |
| 语料同步 413 | OpenResty `client_max_body_size` 未达到 50 MiB |
| 响应媒体类型/编码错误 | 代理压缩、缓存、WAF 错误页或路径改写 |
| H5 刷新页面 404 | 静态站点缺少 `try_files ... /index.html` |
| WebSocket 连接失败 | `/infra/ws` Upgrade/Connection 头、云安全组、合法域名 |
| AI 重复回复 | 两套 Worker 同时启用、旧关键词回复未按租户集成状态跳过、医生抢答仲裁异常 |
| 容器启动正常但没有医疗接口 | 错误使用了不含 medical 模块的 `yudao-server.jar` |
