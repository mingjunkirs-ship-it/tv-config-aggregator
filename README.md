# TV 配置聚合接口

这是一个可直接运行的 TVBox/影视仓配置聚合服务。它会定时抓取配置源，解析常见的 `sites`、`source`、`providers` 结构，并以 API 地址（或源 key）作为稳定指纹。不同配置接口中指向同一个接口的源会合并，保留所有来源信息；某个地址暂时失败时，会继续提供上一次成功的结果。

## GitHub Actions 自动托管（推荐）

仓库已经包含 `.github/workflows/update-config.yml`，无需服务器：

1. 仓库 `Settings → Pages → Build and deployment` 中将 Source 设为 **GitHub Actions**。
2. 打开 `Actions → Update TV configuration → Run workflow` 执行第一次更新。
3. 后续每 6 小时自动抓取、去重并发布；更新频率可修改工作流中的 cron。

发布后的地址是：

```text
https://<GitHub用户名>.github.io/<仓库名>/config.json
```

`status.json` 会列出每个接口的名称、地址、成功/失败状态和错误信息。部分热门接口返回的是带密文的 TVBox 专用格式或要求授权，聚合器不会绕过访问控制；这类接口会保留在状态报告中，不会影响其他可解析源的发布。

添加或删除配置接口只需编辑根目录的 `sources.json`，提交后会立即触发更新。Action 会缓存 SQLite 快照，因此临时失效的上游不会清除上一次成功抓到的影视源。如果所有上游都失败且没有缓存，发布会停止，避免用空文件覆盖有效配置。

## 可选：本地/API 服务启动

```bash
python3 -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

打开 `http://localhost:8000/docs` 可以查看交互式 API 文档。默认会写入你提供的 16 个配置地址，首次启动后台会自动同步，之后按每个源的 `interval_minutes` 刷新。

## 主要接口

- `GET /api/v1/config.json`：统一后的 TVBox 配置，可直接作为配置地址；支持 `q` 和 `group` 筛选。
- `GET /api/v1/providers`：合并后的源列表，支持分页。
- `GET /api/v1/sources`：配置源及同步状态。
- `POST /api/v1/sources`：添加配置源，JSON 示例：`{"name":"我的源","url":"https://example.com/tv.json","interval_minutes":360}`。
- `PATCH /api/v1/sources/{id}`：修改名称、地址、启用状态、刷新周期或请求头。
- `POST /api/v1/sources/{id}/sync?force=true`：立即同步一个源。
- `POST /api/v1/sync`：立即同步当前到期的所有源。

返回的每个合并源包含 `sources`、`sourceCount` 和 `aliases` 字段，便于追踪同一源来自哪些配置接口。原始扩展字段会保留在 `ext`，发生冲突时额外提供 `extVariants`。

## 说明

服务只负责聚合公开配置描述，不代理播放内容。生产部署时建议将管理类写接口放在反向代理鉴权之后，并把 `TV_AGGREGATOR_CORS` 改为实际前端域名。
