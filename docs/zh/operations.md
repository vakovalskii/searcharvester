# 运维

启动后，如何与这个技术栈打交道。下文的命令假设你位于仓库根目录。

## 日常命令

| 操作 | 命令 |
|---|---|
| 后台启动技术栈 | `docker compose up -d` |
| 停止 | `docker compose stop` |
| 停止并删除容器 | `docker compose down` |
| 删除容器 + volumes（清空缓存） | `docker compose down -v` |
| 修改代码后重新构建适配器 | `docker compose build tavily-adapter && docker compose up -d` |
| 查看状态 | `docker compose ps` |
| 查看所有服务日志 (follow) | `docker compose logs -f` |
| 查看单个服务日志 | `docker compose logs -f tavily-adapter` |
| 重启单个服务 | `docker compose restart tavily-adapter` |
| 进入容器 | `docker compose exec tavily-adapter sh` |

修改 `config.yaml` 之后，两个服务都需要重启以重新加载文件：

```bash
docker compose restart searxng tavily-adapter
```

## 健康检查

```bash
# 适配器
curl -sf http://localhost:8000/health && echo OK

# SearXNG
curl -sf "http://localhost:8999/search?q=ping&format=json" | jq '.results | length'

# Docker 级健康检查
docker inspect --format='{{.State.Health.Status}}' tavily-adapter
```

## 冒烟测试

适配器内置 `simple_tavily_adapter/test_client.py`：

```bash
docker compose exec tavily-adapter python test_client.py
```

或在宿主机执行（前提是本地已安装了依赖）：

```bash
cd simple_tavily_adapter && python test_client.py
```

## 日志与调试

适配器日志包含 `request_id`、响应耗时和结果数量：

```
INFO:main:Search request: 比特币价格
INFO:main:Search completed: 3 results in 1.42s
```

当出现问题时：

1. **`results[]` 为空** 通常意味着 SearXNG 没能访问到搜索引擎。直接排查：
   ```bash
   docker compose exec searxng wget -qO- "http://localhost:8080/search?q=test&format=json" | head -c 500
   ```
2. **适配器返回 504 Gateway Timeout** → SearXNG 超过 30 秒无响应。查看其日志 (`docker compose logs searxng`)，某个引擎可能被封。把它在 `config.yaml` → `engines:` → `disabled: true` 中禁用。
3. **500 Internal Server Error** → 查看适配器日志：`docker compose logs tavily-adapter | tail -50`。
4. **`raw_content: null` 全都为空** → 目标网站屏蔽了适配器的 User-Agent，或超时太短。可以这样修正：
   ```yaml
   adapter:
     scraper:
       timeout: 20
       user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ..."
   ```

## 常见问题

### 重启后 `/extract` 对 `{id}/{page}` 返回 404

`/extract` 的内存缓存位于 `tavily-adapter` 进程内，在进程重启或 30 分钟空闲后都会失效。执行 `docker compose restart tavily-adapter` 后，旧的 `id` 全部失效 — 客户端必须重新 `POST /extract`。这是预期行为。

如果这成为问题 — 参考 CLAUDE.md，持久化缓存方案（SQLite / Redis）出于简洁考虑有意未实现。

### 被 Google "Forbidden" / 验证码

SearXNG 访问 Google 时不带 cookies，且按 IP 限流。如果同网段发出大量请求，Google 就会开始要求验证码。可选方案：

- 禁用 Google (`engines: - name: google, disabled: true`)，保留 DuckDuckGo + Brave。
- 启用其他引擎 (`yandex`、`mojeek` 等 — 参考 [SearXNG 文档](https://docs.searxng.org/))。
- 让 SearXNG 走代理（在其 `settings.yml` 中配置）。

### 端口 8000 或 8999 被占用

修改 `docker-compose.yaml` 中的端口映射：

```yaml
tavily-adapter:
  ports:
    - "8010:8000"    # 宿主:容器
searxng:
  ports:
    - "0.0.0.0:9000:8080"
```

如果改了内部端口，别忘记同步更新 `adapter.searxng_url`。

### 适配器看不到 SearXNG

在 docker-compose 中，服务名就是 docker 网络内的主机名。默认值为：

```yaml
adapter:
  searxng_url: "http://searxng:8080"
```

如果适配器在本地运行（不在 docker 中），使用 `http://localhost:8999` — SearXNG 对外发布的宿主机端口。

### `git pull` 后适配器没更新

适配器镜像是本地构建的。Docker Compose 不会自动重新构建：

```bash
docker compose build tavily-adapter
docker compose up -d
```

或用一条命令：`docker compose up -d --build`。

### 忘了复制 `config.yaml`

SearXNG 启动后立即因 `settings.yml` 报错退出；适配器靠 `config_loader.py` 中的 fallback 默认值运行（缺少 `searxng_url` 就无法工作）。解决方法：

```bash
cp config.example.yaml config.yaml
# 修正 secret_key
docker compose restart
```

## 更新镜像

```bash
docker compose pull searxng redis           # 拉取更新
docker compose build tavily-adapter         # 改过适配器时
docker compose up -d
```

SearXNG 升级主版本时请阅读其 release notes — `settings.yml` 的字段可能发生变化。

## 生产上线 checklist（若确有需要）

若打算把技术栈对外暴露：

- [ ] 在 `config.yaml` 启用 `limiter: true` 并配置 `searxng/limiter.toml`。
- [ ] 在适配器和 SearXNG 前面放置 Caddy / nginx + TLS（仓库中有 `Caddyfile`，但**未**接入 `docker-compose.yaml` — 需要手动添加服务）。
- [ ] 加上鉴权（通过 Caddy 做 Basic Auth，或把适配器放到 JWT gateway 之后）。
- [ ] 限制 SearXNG 的对外暴露 — 对公网只暴露适配器。
- [ ] 在 `docker-compose.yaml` 中把 `SEARXNG_BASE_URL` 设为真实域名。
- [ ] 将密钥放入 `.env`，不要把 `config.yaml` 提交到仓库。
- [ ] 配置日志轮转（目前 `max-size: 1m, max-file: 1`，开发环境合适，生产偏少）。

## 归档 / 备份

volumes 中没有有价值的数据 — 只有 SearXNG 缓存和 Valkey 状态。可以安全执行 `docker compose down -v`，不会丢失数据。

例外是你自己的 `config.yaml`。如果里面有独特的配置或 `secret_key`，请把它保存在密钥管理器 / 私有仓库中。
