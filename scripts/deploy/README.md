# 构建脚本(本地打镜像)

三个镜像各自一个脚本,集中放此目录。只做本地 `docker build`,不上传。

| 脚本 | 镜像 | 容器(端口) | 构建上下文 |
|------|------|------------|-----------|
| `build_app.sh` | `ccapps:slim` | ccapps-app(8010→8001) | 仓库根 / `deploy/docker/Dockerfile.slim` |
| `build_mcp.sh` | `ccapps-mcp:latest` | ccapps-mcp(9001→9000) | `deploy/mcp/`(17 工具) |
| `build_mcp_aidt.sh` | `ccapps-mcp-aidt:latest` | aidt_ccappss_mrp_mcp(9005→9000) | `deploy/mcp-aidt/`(12 工具) |

```bash
./scripts/deploy/build_app.sh          # ccapps:slim
./scripts/deploy/build_mcp.sh          # ccapps-mcp:latest(9001)
./scripts/deploy/build_mcp_aidt.sh     # ccapps-mcp-aidt:latest(9005,独立 tag)
```

## 要点
- **9005 独立镜像**:`ccapps-mcp-aidt:latest`,与 9001 的 `ccapps-mcp:latest` 分开,互不覆盖。
- **只构建不上传**:需要上传到 mc 时手动执行:
  `docker save <镜像> | gzip | ssh mc 'gunzip | docker load'`
  (mc sshd 不稳,大镜像建议先 `docker save|gzip > x.tar.gz` 再 rsync 断点续传)
