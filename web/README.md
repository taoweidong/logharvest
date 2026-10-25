# LogHarvest Web

LogHarvest 前端，派生自 [vue-pure-admin](https://github.com/pure-admin/vue-pure-admin)（MIT，
Copyright (c) 2020-present, pure-admin）。本目录保留上游 `LICENSE` 原文；框架层（layout/router/
store/http/components/style）来自该模板，业务页面为 LogHarvest 自有代码。

移植时删除了模板的演示业务（system/monitor/permission/result/guide/about/welcome 等视图、
`src/api/system`、裁剪/二维码/打字机/图片验证码等组件）与重型插件（echarts、vxe-table、
plus-pro-components、intro.js、version-rocket、localforage、cropperjs、qrcode、typeit），
以及 `public/` 下未被引用的音视频与 wasm 演示资源。

## 开发

```bash
cd web
pnpm install
pnpm dev          # http://localhost:8848，/api 代理到 http://127.0.0.1:8000
pnpm typecheck
pnpm build
```

后端接口见 `service/README.md`。登录需要先用 `python -m scripts.seed_admin` 建管理员。

## 说明

- 菜单与路由在前端静态定义（`src/router/modules/*.ts`），没有服务端动态菜单。
- `public/platform-config.json` 与 `types/*.d.ts` 是框架启动与 `@/` 别名类型的必需品，删除会导致
  `main.ts` 启动报错或 typecheck 失败。
- 本 fork 未复制上游的 eslint/stylelint/husky/playwright 配置与依赖，因此没有 `pnpm lint`；
  质量门槛为 `pnpm typecheck` 与 `pnpm build`。
