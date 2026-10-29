# LogHarvest 前端镜像：Node 构建 + Nginx 运行（非 root，监听 8080）。
# 构建上下文 = web/

FROM node:22-bookworm-slim AS builder

WORKDIR /app
RUN corepack enable

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

FROM nginx:1.27-alpine AS runtime

# 非 root：完整替换主配置（pid/缓存/临时目录全部落 /tmp），容器内以 nginx 用户(uid 101)监听 8080
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/ >/dev/null 2>&1 || exit 1
