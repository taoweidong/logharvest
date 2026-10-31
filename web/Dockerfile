# LogHarvest 前端镜像：Node 构建 + Nginx 运行（非 root，监听 8080）。
# 构建上下文 = web/
# 基础镜像约束：生产环境只提供 node:20 / nginx:1.20 等白名单镜像（见 docs/deployment-docker.md §3）。
# node:20 满足 web/package.json 的 engines（^20.19.0）；nginx.conf 无 1.20 不支持的指令。

FROM node:20 AS builder

WORKDIR /app
RUN corepack enable

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

FROM nginx:1.20 AS runtime

# 非 root：完整替换主配置（pid/缓存/临时目录全部落 /tmp），worker 进程以 nginx 用户(uid 101)
# 运行并监听 8080；主进程以官方默认的 root 启动，仅负责打开 /dev/stdout 日志与预建临时目录
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080
# nginx:1.20（Debian 版）不带 wget/curl，健康检查用 bash 内建 /dev/tcp 发一次 GET 并断言 200
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/8080 && printf "GET / HTTP/1.0\r\n\r\n" >&3 && grep -q " 200 " <&3'
