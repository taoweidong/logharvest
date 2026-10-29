# LogHarvest 前端 UI 地图（Playwright E2E 参考）

> 调研方式：只读源码（`E:\GitHub\think\web`），不修改任何文件。所有文本均给 `file:line` 证据。
> 代码中**没有任何 `data-testid`**（全仓 grep 未找到），定位只能靠文案 / Element Plus DOM 结构。
> 行号对应 2026-10-29 工作树（HEAD = ff23e94）。所有路径相对仓库根。

## 0. 部署与定位总则

- 入口 `http://127.0.0.1:18080/`：宿主端口映射到容器 nginx（监听 8080），`location /api/ { proxy_pass http://server:8000; }`（`web/nginx.conf:58-71`，SSE 段 `proxy_buffering off`、`proxy_read_timeout 3600s`）。
- **生产是 hash 路由**：`VITE_ROUTER_HISTORY="hash"`（`web/.env.production` 与 `.env.development` 均为 hash；解析在 `web/src/router/utils.ts:422` `getHistoryMode`）。业务 URL 形如 `http://127.0.0.1:18080/#/logharvest/jobs/3`。`page.goto` 必须带 `#`。
- **API base 硬编码同源 `/api`**：`baseURL: "/api"`（`web/src/utils/http/index.ts:27`），timeout 10000ms（:30）。没有 VITE_* API 地址变量；`web/public/platform-config.json`（:1-28）只含 Title「LogHarvest」/Locale「zh」/ThemeMode「light」/Layout「vertical」/KeepAlive 等，不含 API 地址。结论：E2E 不需要注入 API base，一切请求都打到 `http://127.0.0.1:18080/api/...`。
- 响应形态：仅认证接口（`/api/login`、`/api/refresh-token`、`/api/auth/routes`、`/api/logout`）返回 `{code,message,data}` 信封（`web/src/api/types.ts:4`、`web/src/api/user.ts:8-10`、`:13-14`、`:17-18`、`:26-27`、`:30`）；业务接口是裸 REST 对象/数组且**没有 total**（`web/src/api/logharvestAdmin.ts:3-9`），所以**所有列表页没有 el-pagination**，只有「上一页 / 下一页」窗口式按钮（matches 页是游标式，只有「下一页」）。
- Element Plus 通用 DOM 速查：
  - 对话框：`append-to-body` + `:close-on-click-modal="false"` → `page.getByRole('dialog')`；底部按钮在 `.el-dialog__footer`。
  - 确认框（ElMessageBox.confirm）→ `.el-message-box`（标题 `.el-message-box__title`），按钮文案是各页自定义的，见下文。
  - 抽屉 → `.el-drawer`；底部 `.el-drawer__footer`。
  - el-select 下拉项挂在 body 级 popper：`.el-select-dropdown__item`，按可见文本匹配（状态/级别多为英文码原文）。
  - ElMessage → `.el-message`，文案见各页「成功提示」。
- **`@pureadmin/table` 3.3.0 关键坑**：内容插槽只有当列定义显式写了 `slot` 字段才渲染；不写 `slot` 时与 prop 同名的插槽被整体忽略、单元格退回裸文本（注释见 `web/src/views/logharvest/jobs/index.vue:143-146`、`jobs/detail.vue:87-92`、`workers/index.vue:179-183`、`stats/index.vue:107`）。对 Playwright 的意义：本项目里凡带按钮/标签的单元格都存在且可按文案点击，但**纯文本列就是单元格文本本身**，不要指望额外包了一层。
- 窄屏差异：多处页面按 `appStore.device === "mobile"` 隐藏非关键列、操作列取消 `fixed: "right"`（`jobs/index.vue:147-182`、`workers/index.vue:184-246`、`audit-logs/index.vue:152-181`、`stats/index.vue:105-125`）。真实 Chrome 桌面视口（≥1280）走桌面分支，本文所有列清单均按桌面分支给。
- 时间格式：`formatDateTime` = `YYYY-MM-DD HH:mm:ss`（`web/src/views/logharvest/format.ts:61-65`）；相对时间 `fromNow` = 「N 秒前 / N 分钟前 / N 小时前 / N 天前」（:68-77）；空值统一 `UNKNOWN_TEXT = "—"`（:14）。状态标签显示**后端英文码原文**（pending/running/…，`statusTagType` 只决定颜色，:81-92）。

## 1. 登录页 `#/login`

路由：静态注册 `/login`（`web/src/router/modules/remaining.ts:5-13`，meta.title = i18n `menus.pureLogin`）。白名单只有 `["/login"]`（`web/src/router/index.ts:118`）；无 token 访问任何业务路径 → 重定向 `/login`（:230-235）。

表单控件（`web/src/views/login/index.vue`）：

| 控件 | 渲染文案 | 定位建议 | 证据 |
|---|---|---|---|
| 用户名输入 | placeholder「账号」（i18n `login.pureUsername`，`web/locales/zh-CN.yaml:207`） | `page.getByPlaceholder('账号')` | :186-193 |
| 密码输入 | placeholder「密码」（i18n `login.purePassword`，yaml:208） | `page.getByPlaceholder('密码')`，type=password | :199-207 |
| 验证码 | **未找到**（无 captcha 元素，全文件无验证码逻辑） | — | — |
| 记住登录 | 复选框（i18n 文案块 `web/locales/zh-CN.yaml:206-248`，含 `pureRememberInfo`「勾选并登录后，规定天数内无需输入用户名和密码会自动登入系统」:211）+ 原生 `<select>` 选项 1/7/30 + 尾缀「天内免登录」 | checkbox 用 `.el-checkbox`；天数 select 是**原生 select**（非 el-select），`page.locator('select').selectOption('7')` | :214-240 |
| 登录按钮 | 「登录」 | `page.getByRole('button', { name: '登录' })` | :242-251 |

校验与提示：

- 前端规则（`web/src/views/login/utils/rule.ts:12-37`，只校验必填）：空账号 → 「请输入账号」，空密码 → 「请输入密码」（文案实为 i18n `login.pureUsernameReg/purePassWordReg`，`web/locales/zh-CN.yaml:237-238`；渲染为字段下方红字，`.el-form-item__error`）。
- 成功：`POST /api/login`（`web/src/api/user.ts:13-14`，`{username,password}`），存 token 后 ElMessage「登录成功」（`login/index.vue:74`；文案 i18n `login.pureLoginSuccess`，`web/locales/zh-CN.yaml:229`）。
- 失败：显示后端 `detail` 原文，兜底「登录失败」（`login/index.vue:78-80`；i18n `login.pureLoginFail`，yaml:230）。
- Enter 键触发防抖提交（:90-103）。
- 登录成功跳向：`route.query.redirect` → 否则第一个可见菜单 path（`getTopMenu(true)?.path`）→ 都没有则 `/access-denied`。即：admin/operator 登录后落首页 `#/home`。
- Token 存储（`web/src/utils/auth.ts:23-24、:57-79`）：`accessToken/expires/refreshToken` 写入 cookie `authorized-token`，用户信息（username/roles/permissions）写 localStorage `user-info`；登录态另一判据是 cookie `multiple-tabs`（:30，`router/index.ts:151`）。**Playwright 清理登录态要同时清 cookie 与 localStorage**。

## 2. 动态菜单与角色可见性

菜单**不再来自前端静态模块**（`web/src/router/modules/` 现存仅 `home.ts` 与 `remaining.ts`）；业务路由全部由后端 `GET /api/auth/routes` 下发（信封 `data` 为菜单树，`web/src/api/user.ts:20-27`；`initRouter` 在登录后与刷新页时拉取，`web/src/router/utils.ts:234`）。唯一静态业务入口是首页：`/` → `/home`，name `Welcome`，标题「概览」，`meta.auths: ["system:read"]`，组件复用运行概览页（`web/src/router/modules/home.ts:13-37`）。

菜单种子在后端 `service/src/application/services/rbac_seed.py`（幂等，内置行 name/path/component 受保护）。渲染层级（zh 标题）：

- **任务管理** `/logharvest`（:32-43，title :38；旧库默认名「日志分析」会被重塑为「任务管理」:42）
  - 任务创建 `/logharvest/jobs`（:69-79，组件 `logharvest/jobs/index`；页面内表格条标题仍是「分析作业」——菜单名与页面标题不同，定位时别混）
  - 作业详情 `/logharvest/jobs/:id`（:80-89，**show_link: false**，菜单不可见但路由可达）
  - 任务统计 `/logharvest/stats`（:90-99）
  - 日志分析 `/logharvest/matches`（:100-110；页面内标题「命中结果」，菜单叫「日志分析」）
  - 数据源管理 `/logharvest/datasources`（:111-121）
  - Worker 管理 `/logharvest/workers`（:122-131）
  - 运行概览 `/logharvest/overview`（:132-141，**show_link: false**）
- **规则管理** `/rules`（:44-53）
  - 规则维护 `/rules/maintenance`（:142-151，组件 `rules/maintenance/index`）
  - 规则集维护 `/logharvest/rulesets`（:152-162，path 刻意留在 /logharvest 下保旧书签:66-68 注释）
- **系统管理** `/system`（:54-63）
  - 用户管理 `/system/users`（:163-172）
  - 角色管理 `/system/roles`（:173-182）
  - 菜单管理 `/system/menus`（:183-192）
  - 操作日志 `/system/audit-logs`（:193-202）
- 权限节点（menu_type=2，不显示在菜单，作为 `permissions` 码下发）：`jobs:read 查看作业 / jobs:write 创建作业 / jobs:operate 操作作业 / rulesets:read 查看规则集 / rulesets:write 维护规则集 / datasources:read 查看数据源 / datasources:write 维护数据源 / matches:read 查看命中结果 / workers:read 查看 Worker / workers:operate 操作 Worker / system:read 查看系统状态 / system:operate 执行系统维护 / users:read 查看用户 / users:write 维护用户 / roles:read 查看角色 / roles:write 维护角色 / menus:read 查看菜单 / menus:write 维护菜单 / audit:read 查看操作日志`（:206-246；码值定义 `service/src/domain/permissions.py:20-51`）。

角色授权（种子内置角色，`rbac_seed.py:446-464`）：

- `admin`（名称「管理员」，:433-440）：通配 `*:*:*`（`permissions.py:17`），全菜单。
- `operator`（名称「运维人员」:447-455；菜单清单 :248-272）：任务管理全部 + 规则管理两页 + 操作日志（audit:read）；**无 /system 目录**（无用户/角色/菜单管理）；有 jobs:write/operate、workers:operate、system:operate，但**无 rulesets:write / datasources:write / users:write** → 规则维护页只读、「新建」按钮隐藏。
- `viewer`（名称「只读用户」:456-464；菜单 :274-292）：只读页面可见，但**无操作日志**、无任何 write/operate → 「新建任务」「取消」「排空」等按钮全部隐藏。
- 无任何角色的用户：`wholeMenus.length === 0` → 强制 `router.push("/access-denied")`（`web/src/router/index.ts:191-196`）。
- 直达越权 URL：`meta.roles` 不匹配 → `/access-denied`（:151-155）；`meta.auths` 权限码不含且非通配 → `/access-denied`（:156-166）。`/access-denied` 即 403 全屏页（`remaining.ts:14-23`，组件 `views/error/403.vue`，正文「抱歉，你无权访问该页面」403.vue:48，按钮「返回首页」:67）。
- **但上面 403 只适用于带 `meta.roles/auths` 的静态路由**。动态业务页是后端 `GET /api/auth/routes` 授权后才注册（`service/src/application/services/navigation.py`：未授权菜单根本不进路由表，页面 `meta.auths` 恒为“该用户全部权限码”，不做逐页判权）——所以**无权限直达动态页会落入 `addPathMatch` 的 catch-all `/:pathMatch(.*)*` → 全屏 404 页**（`views/error/404.vue`，正文「抱歉，你访问的页面不存在」:48、「返回首页」:67；浏览器标题 i18n `menus.purePageNotFound`＝"404"，`web/locales/zh-CN.yaml:89`；catch-all 注册 `web/src/router/utils.ts:159-173`）。E2E 断言“viewer 直达 /system/users”等场景时，期望是 **404 文案**而不是 `/access-denied`。
- 按钮级隐藏用指令 `v-perms="'xxx:yyy'"`（判据 `hasPerms`，`web/src/utils/auth.ts:141-155`：通配或包含即放行）。E2E 断言「不可见」时按权限码查上表。

菜单搜索框（顶部）placeholder「搜索菜单（支持拼音搜索）」（i18n `search.purePlaceholder`，`locales/zh-CN.yaml:30`）。

## 3. 运行概览（首页）`#/home` 与 `#/logharvest/overview`

同一组件 `web/src/views/logharvest/overview/index.vue`（静态路由 title「概览」；动态路由菜单名「运行概览」、show_link=false）。

- 标题 h2「运行概览」（:171）；右上「刷新」按钮（:176）与「数据截至 HH:mm:ss / 加载中…」文本（:173-175）。
- 「执行维护」按钮（:177-184，`v-perms="'system:operate'"`，operator/admin 可见）：确认框标题「执行维护」（:143）；成功 ElMessage「维护完成：回收 X 个文件任务，降级 Y 台 Worker，收尾 Z 个作业」（:153-156），并在页内留下 alert「上次维护：回收…」（:204）。
- 四张统计卡：「作业总数」（:210）、「24 小时命中行数」（:219）、「存活 Worker」（:228）、「最老 pending 等待」（:234；超过阈值秒数高亮 :21）。
- 概览接口不可用时降级 alert（:112 起，文案含「"作业状态分布 / 24 小时命中数 / Worker 版本分布"依赖概览接口」）。
- API：`GET /api/system/overview`，退回 `GET /api/system/status`（`home.ts:5-11` 注释、`web/src/api/logharvest.ts` listSystemStatus/overview）。
- SSE/轮询：**无**。进页拉一次，只有右上文字「刷新」按钮手动重取（全 `web/src/views/` 中 `setInterval` 仅存在于 `jobs/detail.vue:51/:294`，SSE 仅 §6 用到；E2E 验证这一点可断言除 §6 外任何页面 networkidle 都会到达）。

## 4. 任务创建（作业列表）`#/logharvest/jobs`

页面 `web/src/views/logharvest/jobs/index.vue`。PureTableBar 标题「分析作业」（:219）。工具条（`#buttons`，:220-266）：

1. 关键字输入：placeholder「关键字（本页）」（:226）——**只过滤当前页**（`displayRows` 前端过滤 :56-65，后端 `/jobs` 无关键字参数，见页脚说明 :332-338）。
2. 状态下拉：placeholder「状态筛选」（:237），选项为英文码 `pending / running / succeeded / partial / failed / cancelled`（`JOB_STATUS_OPTIONS`，`web/src/api/logharvestTypes.ts:304-311`），选择后**下推后端 `?status=`** 并回到第一页（:239、:76、:87-90）；非法 status 后端 422。
3. 「UTC 日：xxxx-xx-xx」可关闭标签（仅当 URL 带 `?date=` 时出现，:249-257；关闭时同步清 URL query :102-106）。
4. 「新建任务」按钮（:258-264，`v-perms="'jobs:write'"`）→ 打开创建向导（§5）。

表格列（顺序，:147-182）：`ID / 名称 / 状态(el-tag 英文码) / 文件数 / 总字节(GiB 格式化 formatGB) / 创建时间(YYYY-MM-DD HH:mm:ss) / 操作(fixed right)`。

行操作：

- 「详情」（:292-299）→ `#/logharvest/jobs/{id}`。
- 「取消」（`v-perms jobs:operate`，:300-326）：非终态可点，终态 disabled + tooltip「作业已进入终态，后端不会改写」（:302）。确认框：正文「确认请求取消作业 #N（名称）？已进入运行中的文件任务会跑完后收尾。」标题「取消作业」，按钮「确认取消」/「再想想」（:110-118）。成功 ElMessage「作业 #N 当前状态：{status}」（:124）。API `POST /api/jobs/{id}/cancel`。

翻页：limit 50（:35）；底部「上一页」（offset=0 时 disabled）/「下一页」（本页取满 50 才 enabled）（:340-345）；无总数文案「列表接口不返回总数： 第 N 页，本页 M 条，其中符合提示条件 K 条（截至 HH:mm:ss）」（:332-338）。

URL query 消费（:186-209）：`?status=`（合法值生效）、`?date=YYYY-MM-DD`（生成 UTC 日标签）、`?data_source_id=N`（自动打开向导并预填第①步，需 jobs:write）。空表：pure-table 默认「暂无数据」。

SSE/轮询：**无**。进页/翻页/改筛选各发一次 `GET /jobs`；作业状态变化不会自动反映到列表，E2E 想看到 pending→running 需点 PureTableBar 的刷新图标（icon-only，定位见 §18）或重新 goto。

## 5. 作业创建向导（3 步 el-steps）

`web/src/views/logharvest/jobs/create-dialog.vue`。对话框标题「新建分析任务」（:286），width `min(680px, 94vw)`（:287）。步骤条（:291-295）：①「数据源与目录」②「规则集」③「过滤与确认」。底部固定文案「{当前步} / 3 · 提交后直接进入作业详情并跟随实时进度」（:453-455）；按钮「取消」/「上一步」/「下一步」/「提交」按步显隐（:457-467）。

- **第①步**（表单 label 100px）：
  - 「作业名称」输入，placeholder「例如：web01 一周错误日志」（:304-306）。
  - 「数据源」下拉 placeholder「选择挂载点」（:307-315）；选项文本 `{name}（{root_path}）`，停用的追加「 已停用」（:312）。数据源来自 `GET /api/data-sources`（limit 500，`web/src/api/logharvestAdmin.ts:26-27`）。选中停用数据源出现红字「该数据源已停用，后端提交时会返回 404（data source not found or disabled）」（:316-318）。
  - 「扫描目录」输入，placeholder「数据源 root_path 之内的绝对路径」（:320-325）；下方灰字「必须位于 {root_path} 之内，且是已存在的目录；符号链接与点目录会被跳过」（:93-97）。
  - 「路径判定」按钮「测试数据源根路径」（未选数据源时 disabled，:332-340）→ `POST /api/data-sources/{id}/test`；结果标签「存在/目录/可读: ✓|✗」+ 只读判定「可写：后端会拒绝处理（409）」/「只读已确认」/「只读未确认（平台判不出来）」（:345-359、:183-191）。
  - 无数据源时 alert「还没有任何数据源」+ 链接按钮「去建数据源」→ `#/logharvest/datasources`（:364-377、:277-280）。
  - 校验（:70-74）：「请填写作业名称」/「请选择数据源」/「请填写要扫描的目录」。
- **第②步**：
  - 「规则集版本」下拉（filterable，:387-395）；选项 `{name} v{version}（#{id}）`（:392），来自 `GET /api/rule-sets?limit=200`（:133-136）。
  - 「规则条数」标签：选中后按需 `GET /api/rule-sets/{id}` 取 config.rules.length；显示「N 条」/「加载中…（列表接口不返回规则内容，选中后才取详情）」/「条数获取失败（不阻断提交）…」/「未选择…」（:107-112、:397-404）。
  - 校验：「请选择规则集版本」（:76-78）。
- **第③步**：
  - 「递归扫描」switch（默认开，:409-411）；「最大深度」input-number 1-20 默认 5（:412-415）；「文件后缀」输入 placeholder「留空=后端默认的全部压缩包后缀，例如 .gz .tar.gz」（:416-425，逗号/空格混分隔 :99-104）；「最大文件数」input-number 1-20000 步长 1000 默认 20000（:427-429）。
  - 「提交摘要」descriptions 四行：`数据源：…（#id） / 扫描目录：… / 规则集：… v…（#id，N 条） / 文件过滤：递归=是|否，深度=…，上限=…[，后缀=…|后缀=后端默认全部压缩包]`（:114-127、:433-437）。
- 提交：`POST /api/jobs`，请求头 **`Idempotency-Key` 由前端生成** `crypto.randomUUID()`（`web/src/api/logharvest.ts` :66-71 附 timeout 60000；对话框 `:68`），**同一轮提交复用同键**，仅在“打开对话框 / 提交成功 / 任意表单改动”时重新生成（:63-68、:170、:177-180、:264）。请求体：`{name, data_source_id, source_path, rule_set_id, file_filter:{recursive,max_depth,max_files,suffixes|null}}`（:245-258）。
- 失败：页内 error alert「提交未成功」+ 按状态码加前缀的 detail 原文：400「路径不合法：」/409「幂等冲突或目录不可用：请求内容已变更（…）」/422「参数校验失败：」/503「目录扫描超过 10 秒：」（:232-239、:440-449）。
- 成功：ElMessage「作业 #N 已创建，共 M 个压缩文件」（:261-263）→ 关闭对话框并**跳转 `#/logharvest/jobs/{id}`**（emit created → `jobs/index.vue:132-136`）。

## 6. 作业详情 `#/logharvest/jobs/:id`

`web/src/views/logharvest/jobs/detail.vue`（菜单隐藏页，只能从列表「详情」/命中页链接/直接 URL 进入；路由级要求 `jobs:read`——由后端授权该页面路由，admin 通配）。

头部卡片（:434-516）：

- 标题模板「作业 #{id} · {name}」（加载中显示「加载中」）（:438）。
- 状态 el-tag：英文码原文（:441-443）；流连接状态标签「实时进度」（:444）/「轮询降级」（:445-447）。
- 按钮组：「查看命中结果」（:448-450 → `#/logharvest/matches?job_id={id}`，:406-408）；「取消作业」（仅非终态 + `v-perms jobs:operate`，:451-459）确认框正文「确认请求取消该作业？」标题「取消作业」（默认按钮 确定/取消，:356）；成功 ElMessage「已请求取消，作业状态：{status}」（:365）。「重试失败文件（N）」（`failedCount>0` 才出现 + `v-perms jobs:operate`，:460-468）确认框正文「确认重试该作业全部失败的文件任务？失败任务会重置为待处理并重新排队。」标题「重试失败文件」（:374-378）；成功 ElMessage 三态：「已重新排队 N 个失败文件」/「没有可重试的失败文件任务」(info)/「已重新排队 N 个失败文件，仍有 M 个待再次点击重试」(warning)（:385-393）。API：`POST /api/jobs/{id}/cancel`、`POST /api/jobs/{id}/files/retry-failed` → `{retried, remaining_failed}`（`web/src/api/logharvest.ts`）。
- descriptions 字段（:473-496）：数据源 `#id` / 规则集 `#id` / 来源目录 / 文件数 / 总字节(GB) / 请求取消 是|否 / 开始时间 / 结束时间 / 创建人 / 错误。
- 进度行文本「进度 X / Y · 已处理行数 N · 命中行数 M」（:499-503）+ `el-progress`（:504）+ by_status 计数标签「{status}: {count}」（英文码，:505-514）。

实时进度（SSE）：`onMounted` 即 `startStream()`（含终态作业，:410-427）；`streamJobProgress` 用 `fetch + ReadableStream` 读 `GET /api/jobs/{id}/events`，请求头 `Accept: text/event-stream` + `Authorization: Bearer <accessToken>`（`web/src/utils/sse.ts:53-65`；不是 EventSource，:4-11 注释）。SSE 断流且作业未终态 → 降级 5 秒轮询 `GET /jobs/{id}`+`GET /jobs/{id}/files`，ElMessage warning「实时进度中断，已改为每 5 秒轮询作业详情」（detail.vue:347）/「实时进度连接已结束，但作业尚未进入终态，已改为每 5 秒轮询」（:337-339），标签改「轮询降级」。

文件任务表（PureTableBar 标题「文件任务」，:519）：工具条「状态筛选」下拉（选项 = JOB_STATUS_OPTIONS 英文码，下推后端 `?status=`，:521-534、:72-76）。列（:93-122）：`ID / 文件 / 状态(tag) / 大小 / 尝试(N / M) / 历史 / Worker / 已处理行 / 命中行 / 心跳 / 错误`。「历史」是 link 按钮（:555-559）→ 打开尝试历史抽屉。底部说明「状态筛选已下推到后端…“历史”打开该任务的尝试记录」+「上一页 / 下一页」（limit 50，:570-583）。

尝试历史抽屉（el-drawer size 920px，:589-691）：

- 标题模板「尝试历史 · 文件任务 #{taskId}（{fileName}）」（无文件名时省略括号，:592-596）。
- 说明段：一行 = Worker 的一次领取；状态 running/succeeded/failed/cancelled/timeout；「“—” 表示该列没有值」（:600-608）。
- 数据 `GET /api/jobs/{job_id}/files/{task_id}/attempts`（裸数组）。错误分支 alert 标题「尝试历史加载失败：以下是接口错误，不代表「该任务没有尝试记录」」+ detail（:610-619；404 特判文案 :179-189）。空数组分支 el-empty「该任务尚无尝试记录（接口返回 200 空数组，未开始执行）」（:666-670）。
- 列（:144-177）：`次序 / Worker / 版本 / 状态(tag，timeout 红色) / 开始时间 / 结束时间 / 已处理行 / 命中行 / 错误`；空值一律「—」（UNKNOWN_TEXT）。
- footer：文本「已加载 N 次尝试，后端按 attempt_no 升序返回」+「重新加载」按钮（:672-689）。
- 深链：命中页点「所属文件」跳来 `#/logharvest/jobs/{id}?file_task={taskId}` 会直接打开该任务抽屉（:238-248）。

## 7. 日志分析（命中结果）`#/logharvest/matches`

`web/src/views/logharvest/matches/index.vue`。PureTableBar 标题「命中结果」（:362）——菜单名是「日志分析」（§2），别按菜单名找页面标题。

查询面板（先选作用域才允许查询，防全表扫描 :49-58）：

- 作用域 radio：「按作业」/「按时间范围」（:221-224）。
- 「按作业」：作业下拉 placeholder「选择要查看的作业」，选项 `#{id} {name}`（:226-241），数据 `GET /api/jobs?limit=200`，失败 warning「作业下拉加载失败，可改用时间范围查询」（:139-150）。
- 「按时间范围」：两个日期时间选择器 placeholder「created_from」「created_to」（:243-258）。
- 「级别」下拉 placeholder「severity」，选项 `info / warning / error`（:260-274）。
- 「每屏」下拉选项 50/100/200/500，默认 50（:275-279）。
- 「查询」按钮：作用域未就绪时 disabled + tooltip（scopeHint 文案 :280-292）；未就绪另有 warning alert（:298-304）。「回到第一屏」按钮（:293-295）。
- 说明段落 :305-315（游标翻页原理）。

列（:168-200，顺序）：`时间 / 所属作业 / 所属文件 / 包内路径 / 行号 / 规则 / 级别 / 数值 / 原始行`。

- 「所属作业」是链接 `#{job_id}` → 作业详情（:376-380）；「所属文件」链接 → `#/logharvest/jobs/{id}?file_task=…`（:381-385）。级别为 tag（:386-390）。原始行可带「已截断」tag，tooltip「后端只保存截断后的原始行（raw_truncated = true）」（:397-403）。

翻页：**游标式**（`GET /api/matches`，created_before+id_before），只有「下一页」，无上一页、无总数；文案「总数未知：第 {段} 段，本段 {n} 条」（不足一屏时追加「（不足一屏，已到末尾）」）（:407-420）。空态「本段没有命中行」/查询前「尚未查询」（:349）。`onMounted` 消费 `?job_id=` 自动按作业查询（:202-213）。

SSE/轮询：**无**；只有点「查询 / 回到第一屏 / 下一页」才发请求，新命中不会自动出现。

## 8. 数据源管理 `#/logharvest/datasources`

`web/src/views/logharvest/datasources/index.vue`。**卡片栅格，不是表格**（无 PureTableBar、无列）。

- 页头「数据源」+ 灰字「企业磁盘挂载点登记；后端强制只读，处理时不会回写源目录」（:138-141）；按钮「刷新」（:144）、「新建数据源」（`v-perms datasources:write`，:145-151）。
- 每张卡：名称 + `#id`（:164-167）、root_path 等宽字体（:168-170）、tag「启用/停用」（:173-174）与「登记只读：是/否」（:176-182）、描述、测试结果标签区（未测试时灰字「路径未测试」:209；测试后「存在/目录/可读: ✓|✗」+ 判定「可写：禁止处理 / 只读已确认 / 只读未确认」:190-210、:72-91）。
- 卡内按钮：「测试路径」（`POST /api/data-sources/{id}/test`，失败清掉旧徽章 :58-69）、「用它建任务」（`v-perms jobs:write` → `#/logharvest/jobs?data_source_id={id}`，自动开向导，:223-225、:119-125）。
- 空态 el-empty「还没有登记任何数据源：先新建一个挂载点」（:230-234）；底部「后端列表接口不返回总数；第 N 页，本页 M 条」+ 上一页/下一页（limit 50，:29、:237-255）。
- 「新建数据源」对话框（title :259，width min(560px,92vw)）：字段「名称」placeholder「唯一名称，重名返回 409」（:266）、「根路径」placeholder「企业磁盘挂载点的服务器绝对路径」（:272）、「只读」disabled 输入框固定值「true（后端强制只读，不提供开关）」（:275-277）、「描述」textarea（:278-280）；校验「请填写数据源名称」「请填写挂载点绝对路径」（:41-44）；按钮「取消 / 创建」（:283-286）；成功 ElMessage「数据源“{name}”已创建」（:106）。`POST /api/data-sources` 体 `{name, root_path, read_only: true, description}`（:99-105）。无编辑/删除入口（未找到）。

SSE/轮询：**无**；「刷新」是文字按钮（:144），「测试路径」只更新该卡徽章，不重取列表。

## 9. Worker 管理 `#/logharvest/workers`

`web/src/views/logharvest/workers/index.vue`。

- 两条顶部 info alert：「这里不执行任何部署」（:268-277）与「CPU / 内存 / 临时盘空闲 / 最近错误：`—` 是「没有上报过」，不是 0」（:279-289）。
- PureTableBar 标题「Worker（按版本分组）」（:291）。工具条：「状态筛选」下拉（选项 `online / busy / draining / offline` 英文码，:294-306、:36-41，**只过滤本页**，:457-464 文案声明）；「版本号」下拉（可自定义输入 allow-create，选项=本页出现的版本集合，:307-317）；「按版本排空」按钮（`v-perms workers:operate`，未填版本号 disabled，:318-326）。
- 数据按 `version` 分组渲染成 N 张卡（每卡一个内嵌表格）：卡头「版本 {v}」+「{n} 台」tag + 「存活（online/busy/draining）x 台 · 离线 y 台」+ link「排空该版本」（v-perms，点击即填版本并走按版本排空确认，:335-363）。
- 组内表列（桌面，:184-246）：`Worker(id) / 主机名 / 状态(tag 英文码) / 当前文件任务(空为 -) / 并发上限 / CPU(x.x%) / 内存(x.x MB) / 临时盘空闲(x.xx GB) / 心跳(相对时间 fromNow，tooltip 绝对时间) / 排空标记(已标记|无) / 最近错误 / 操作(fixed right)`；空值「—」（format.ts:14、:22-58）。
- 行操作（`v-perms workers:operate`）：「排空」——confirm 正文「确认排空 Worker {id}？它跑完当前文件任务后不再领取新任务。」标题「排空 Worker」按钮「排空 / 取消」（:104-110）；成功 ElMessage「{id}：drain_requested={bool}，状态 {status}」（:117-120）；已标记行按钮 disabled（:421）。「恢复」——offline 行 disabled + tooltip「Worker 已离线，后端对离线机器恢复领取返回 409」（:426-442）；成功 ElMessage「{id} 已恢复领取（状态 {status}）」（:133-136）。API：`POST /api/workers/{id}/drain`、`/resume`、`POST /api/workers/drain-version`（`web/src/api/logharvest.ts`，GET /workers limit≤200）。
- 「按版本排空」：空版本 warning「请先填写要排空的 Worker 版本号」（:150-153）；confirm 正文「确认排空版本 {v} 的全部 Worker？离线机器也会被记下标记，重启后生效。」标题「按版本排空」按钮「排空 / 取消」（:155-160）；成功 ElMessage「版本 {v}：已排空 {n} 台，标记 {m} 台离线（重启后生效）」（:167-169）。
- 空态 el-empty「没有可见的 Worker：请先启动 Worker 进程（python -m src.worker.main）」（:330-334）。底部统计长文案 + 上一页/下一页（**limit 100**，:44、:457-479）。

SSE/轮询：**无**。心跳（last_heartbeat）由 Worker 进程写库，页面不会自动刷新；E2E 断言“排空后状态变 draining / 心跳变新”必须手动重进页或点工具条刷新。

## 10. 任务统计 `#/logharvest/stats`

`web/src/views/logharvest/stats/index.vue`。API `GET /api/stats/jobs?days&data_source_id`（`web/src/api/analytics.ts`，days 限定 1-90）。

- 工具行：h2「任务统计」（:136）；radio 按钮组「7 天 / 14 天 / 30 天」默认 14（:22-23、:137-141）；数据源下拉 placeholder「全部数据源」（可清空/搜索，:143-160）；「刷新」按钮（:161）；灰字「日期为 UTC 日界，与本地时间可能差一天」（:162-164）。
- 接口 403（无 jobs:read）整页降级 el-result「无查看权限 / 任务统计需要 jobs:read 权限（接口返回 403），请联系管理员分配后重试。」（:167-172）。
- 四卡：「作业数」（含 5 个**可点击**状态 tag：运行中/成功/部分成功/失败/已取消 + 计数 → 跳 `#/logharvest/jobs?status=`，:94-103、:182-197）、「处理字节」（GB + 「文件任务共 N 个」，:200-208）、「命中行数」（:210-217）、「文件失败率」（`x.x%` 或「未知」，>10% 红色，:219-234）。
- 「每日趋势（{days} 天，条形长度按命中行数归一）」卡（:238-298）：行文本 `{date} 作业 N · 字节 X` + 成/败 tag（点击附加 status 条件）+ 「M 行」+ 进度条；点行跳 `#/logharvest/jobs?date={utc日}`；空态 el-empty「该时间段没有作业」（:242-246）；说明「点击某一行跳「任务创建」列表并带上当日条件；点「成/败」标签额外带上状态条件。」（:289-291）。
- 「Top 规则（按命中行数）」卡（:300-312）：列 `规则 ID /（桌面加）规则版本 / 命中行数 / 涉及作业`（:105-125）；空态「该时间段没有命中」（:311）。

SSE/轮询：**无**；切「7/14/30 天」、换数据源、点文字「刷新」按钮（:161）各自重取 `GET /stats/jobs`。

## 11. 规则集页 `#/logharvest/rulesets`

`web/src/views/logharvest/rulesets/index.vue`（菜单名「规则集维护」，`rbac_seed.py:152-162`；path 刻意留在 `/logharvest` 下兼容旧书签）。后端接口：`GET /api/rule-sets`、`GET /api/rule-sets/{id}` 需 `rulesets:read`（`routes_rule_sets.py:21/:32`），`POST /api/rule-sets` 需 `rulesets:write`（:45）。

- PureTableBar 标题「规则集」（:100）。工具条**唯一**文字按钮「新建规则集」（`v-perms="'rulesets:write'"`，:103-105）→ 不弹对话框，直接 `router.push('/rules/maintenance')` **无 query**（:62-69，跳 §12 维护页新建）。
- 列（:71-93）：`ID / 名称 / 版本 / 规则数 / 创建时间(桌面列 :81-85) / 操作(fixed right)`。「规则数」是 slot：详情返回后显示 el-tag 数字，之前一直是灰字「加载中…」（:117-122；列表接口不返回 config，逐行 `GET /rule-sets/{id}` 补数 :31-44，失败置 null 也停在「加载中…」——E2E 别把它当死锁 bug）。
- 行操作：「详情」（:127-129）打开只读抽屉；「编辑规则」（`v-perms rulesets:write`，:130-138）→ `#/rules/maintenance?rule_set_id={id}&version={v}`。
- 翻页：limit 20（:24）；窗口式「上一页 / 下一页」（:147-154，下一页=本页取满 20 才 enabled）。底部灰字（:142-146）：「后端列表接口不返回总数与规则内容；同名规则集以版本递增共存，第 N 页。规则的增删改与「另存新版本」在「规则管理 → 规则维护」整页完成。」
- 加载失败 ElMessage 兜底「规则集列表加载失败」（:47）。SSE/轮询：**无**。

详情抽屉 `rulesets/edit-drawer.vue`（el-drawer，size `min(860px, 94vw)`、`:close-on-click-modal="false"`，:96-97）：

- 标题：有详情 `规则集 {name} v{version}（#{id}）`，否则 `查看规则集 #{id}`（:91-95）。
- descriptions 八项（:112-132）：`ID(#n) / 名称 / 版本(vN) / 创建时间 / 创建人(空为 -) / 规则条数 / 文件通配 / 编码 {enc} / 回退 {fb}`。
- 内嵌 pure-table 规则列（:49-58）：`规则 id / 展示名 / 严重级 / 启用 / 匹配 / 阈值 / 标签`；「启用/停用」tag success/info（:143-147）；匹配摘要（matchSummary，:32-40）：关键字→`关键字（全部命中|任一命中，区分大小写|忽略大小写）：a、b`；正则→`正则 /pat/（标志 x|无，超时 Nms）`；阈值 `field op value` 或 `-`（:43-47）；标签 `join(", ")` 或 `-`（:154-156）。
- 错误分支：el-alert 标题「详情加载失败」+ pre 逐行 detail（:100-109；兜底文案「规则集详情加载失败」:69）。未选版本：el-empty「未选择规则集版本」（:160）。
- footer 灰字两态（:165-171）：有 `rulesets:write` →「规则编辑已迁到「规则维护」整页：改完另存为新版本（同名版本 max+1）」并出按钮「去维护页编辑」（:174-182，详情未加载时 disabled）；只读 →「当前账号只有只读权限，界面不提供任何写入口」且**无**该按钮。共同按钮「关闭」（:173）。
- 角色差异：operator/viewer 菜单里有本页（`rbac_seed.py:248-272/:274-292`）但无 `rulesets:write` → 「新建规则集」「编辑规则」「去维护页编辑」全部从 DOM 移除（v-perms 是 `removeChild`，见 §18）。

## 12. 规则维护页 `#/rules/maintenance`

`web/src/views/rules/maintenance/index.vue` + `rule-table.vue` + `rule-form.ts`（菜单「规则维护」，`rbac_seed.py:142-151`）。判权 `canWrite = hasPerms("rulesets:write")`（index.vue:28）：无写权限时**整页只读**（el-form :disabled、隐藏保存与行操作列、Tab 标题变化）。接口：页面本身需 `rulesets:read`；保存 `POST /api/rule-sets`（rulesets:write，`routes_rule_sets.py:45`）；跨版本检索 `GET /api/rules`（rulesets:read，`routes_analytics.py:20`）。

Tab 两个：①label 随权限「编辑规则」/「查看规则」（:301）②「跨版本检索」（:409）。

**编辑/查看 Tab**：

- 版本选择：el-select placeholder「选择规则集」（filterable，:305-320）+「选择版本」（:323-336，选项文本 `v{version}（#{id}）`）；右侧灰字「另存为新版本时同名版本号由后端自动 +1」（:338-340）。
- 表单四项（label-width 110px，:351-368）：「规则集名称」输入、「文件通配」输入 placeholder「\*\*/\*」、「编码」、「回退编码」；新建默认 `**/*` / `utf-8` / `gb18030`（:45-51、:83-85）。
- 分隔标题「规则（{N} 条）」（:372-374）→ RuleTable（下）。
- 保存区（仅 canWrite，:393-405）：主按钮「另存为新版本」（disabled=`!selectedId || 规则0条`）+ 灰字「保存后留在当前编辑态，可继续调整；正则编译等校验以后端 422 为准。」；成功 ElMessage「已创建版本 {n}」（:151）；后端错误（含 422 逐行）渲染在页内红色 alert 的 `<pre>`（:383-391，formatDetailError 兜底「保存失败」:157）。
- 加载兜底：「规则集列表加载失败」（:69）、「规则集版本加载失败」（:99）。
- URL query 消费（:266-295）：`?rule_set_id=` 优先直接 loadVersion；否则 `?rule_set_name=&version=` 命中该版本，缺 version 取最新。

**规则表格 `rule-table.vue`（el-table，非 pure-table）**：

- 外层列：`展开行(:72) / #序号(:153) / 规则 id(行内 el-input placeholder「必填」 :154-158) / 严重级(select，选项 info|warning|error，rule-form.ts:30；:159-165) / 启用(switch :166-170) / 匹配方式(radio-button「关键字」「正则」 :171-178) / 匹配配置(摘要 :179-183) / 阈值(:184-188) / 操作(只读隐藏 :189-211)`。匹配配置摘要逻辑在 script :32-40：关键字 → `任一命中 N 词：a、b` 或 `全部命中 N 词：a、b`，0 词时末段为「（空）」；正则 → `/pat/ism · Xms`（空模式显示 `(空)`）。阈值列：开关关「—」、开 `field op value`（值空显示 `?`，:42-46）。操作列按钮「上移」「下移」「删除」（首行上移 disabled、末行下移 disabled）。表下按钮「添加规则」（:213-215，只读隐藏）。校验失败整行红底 class `rule-row-error`（:19-21、:225-227）。
- 展开行字段（:78-149）：「规则 id」placeholder「稳定标识，命中结果里回显」、「展示名」、「标签」placeholder「逗号或换行分隔」；关键字分支：radio「任一命中（any）」「全部命中（all）」、「关键词」textarea placeholder「每行一个（或逗号分隔）；后端要求 any/all 恰好提供一侧」、「区分大小写」switch；正则分支：「正则模式」textarea placeholder「Python 语法，命名分组写 (?P&lt;name&gt;...)」、「标志位」checkbox `i（忽略大小写）/ m（多行）/ s（点匹配换行）`+ hint「后端仅允许 i/m/s 三个标志」、「超时（毫秒）」input-number(min 1, step 50，默认 50)+hint「必须大于 0，防灾难性回溯」；「数值阈值」switch → 「字段（正则命名分组）」/ 运算符 select `gt|gte|lt|lte|eq`（rule-form.ts:31）/「比较值（数值）」+ hint「字段必须是正则里的命名分组，是否成立由后端校验（422 原文回显）；关键字规则不可启用阈值」。
- 删除行确认：ElMessageBox 正文「确认删除规则“{id|序号}”？保存新版本后才会落库。」标题「删除规则」，按钮「删除」/「取消」（rule-table.vue:48-60）。
- 前端校验文案（rule-form.ts:133-156，逐行红字显示在展开行顶部 :75-77；提交时聚合进 backendError alert）：行级「规则 id 必填」「关键词（任一命中）不能为空」「关键词（全部命中）不能为空」「正则模式不能为空」「超时须大于 0 毫秒」「阈值依赖正则命名分组，关键字规则没有可提取字段」「请填写阈值的字段名（正则命名分组）」；表级「至少需要一条规则」「规则 id 不能重复」「规则 {id|(未命名)}：{行级错误}」。

**跨版本检索 Tab**：

- PureTableBar 标题「规则索引」（:410-411）。检索控件：输入 placeholder「规则 ID / 正则 / 关键词」（Enter 触发，:417-423）、el-select placeholder「类型」选项 `keyword / regex`（:424-432）、输入「规则集名称」（:433-439）、按钮「检索」（:440-442）。
- 列（:223-262）：`规则集 / 版本 / 规则 ID / 类型(tag 英文码) / 严重级(tag) / 摘要 / 操作`；行按钮文案随权限：canWrite「去编辑」/ 只读「查看」（:461-470）→ 切回编辑 Tab 并加载该 rule_set_id 版本（:216-221）。
- 底部灰字（:472-479）：已检索「后端列表接口不返回总数；第 N 页，本页 M 条」；未检索「跨版本检索是后端查询期解析 config，未执行检索」；「上一页 / 下一页」limit 20（:171、:480-493）。失败 ElMessage 兜底「规则检索失败」（:195）。
- SSE/轮询：**无**。
- 角色差异：viewer/operator 只见「查看规则」Tab、表单 disabled、无「添加规则/上移/下移/删除/另存为新版本」、行按钮变「查看」。

## 13. 用户管理 `#/system/users`

`web/src/views/system/users/index.vue`；API `web/src/api/rbac.ts`（GET/POST `/users`、PUT/DELETE `/users/{id}`、PUT `/users/{id}/roles`、POST `/users/{id}/reset-password`；PUT/DELETE 返回 204 无 body）。权限：读 `users:read`、写 `users:write`（`routes_users.py:31-32`）。**admin 专属菜单**（operator/viewer 无 /system 目录 → 直达 = 404，见 §2）。

- 搜索区（:337-358）：label「用户名称：」placeholder「搜索用户名 / 昵称」；按钮「搜索」「重置」（keyword 下推后端）。
- PureTableBar 标题「用户管理」（:362）；「新建用户」（`v-perms users:write`，:364-371）。
- 列（:307-321）：`ID / 用户名 / 昵称 / 角色(多 tag，空「未分配」 :387-399) / 状态(「启用」success / 「停用」info :400-408) / 创建时间 / 操作(fixed right)`。
- 行操作（:413-474）：「修改」「分配角色」（users:write）+ More 图标 el-dropdown →「重置密码」「删除」。
- 新建/编辑对话框（title `新建用户`/`编辑用户`，:497）：用户名 placeholder「登录名，唯一」（编辑时 disabled，:508）、昵称「显示名称（可选）」（:512）、初始密码「至少 8 位」（仅新建，:520）、角色多选 placeholder「可选，支持多选」（option label `{name}（{code}）`，:529）、启用 switch。校验（:106-122）：「请填写用户名」「以字母或数字开头，仅含字母、数字、. _ @ -，长度 2-50」「请填写初始密码」「密码至少 8 位」（:119）。成功 ElMessage「用户“{u}”已创建」（:171）/「用户信息已更新」（:177）。
- 分配角色对话框（title「分配角色 - {u}」，:554）：placeholder「选择该用户拥有的角色；不选表示不分配任何角色」（:564）、灰字「提交后会以当前选择覆盖该用户的角色集合。」（:575）；成功「用户“{u}”的角色已更新」（:208-210）。
- 重置密码对话框（title「重置密码 - {u}」，:587）：新密码/确认密码（placeholder「至少 8 位」:604），footer「取消」「重置」（:620）；校验「请填写新密码」「请再次输入新密码」「两次输入的密码不一致」（:238）「密码至少 8 位」（:231）；成功「用户“{u}”的密码已重置」（:263-265）。
- 删除确认：正文「确认删除用户“{u}”？删除后无法恢复。」标题「删除用户」，按钮「删除」(danger)/「取消」（:276-287）；成功「用户“{u}”已删除」（:293）。
- 翻页：limit 20（:40），footer「后端列表接口不返回总数；第 N 页，本页 M 条」（:478）+「上一页/下一页」。
- 失败兜底 ElMessage：「用户列表加载失败」(:53)/「角色列表加载失败」(:64)/「创建用户失败」「更新用户失败」(:182)/「分配角色失败」(:214)/「重置密码失败」(:268)/「删除用户失败」(:300)。SSE/轮询：**无**。

## 14. 角色管理 `#/system/roles`

`web/src/views/system/roles/index.vue`；API rbac.ts（`listRoles` limit 500 全量拉取，rbac.ts:56-57；POST/PUT/DELETE `/roles/{id}`、GET/PUT `/roles/{id}/menus`）。权限：`roles:read`/`roles:write`（`routes_roles.py:26-27`）。admin 专属。

- 筛选区（:354-381）：「角色名称：」placeholder「请输入角色名称」、「角色标识：」placeholder「请输入角色标识」、「状态：」select placeholder「请选择状态」选项「已启用」/「已停用」（:379-380）——**全部前端过滤**已加载的全量列表（`filters.ts:39-52`），不发请求。
- PureTableBar 标题「角色管理」（:405）；「新建角色」（`v-perms roles:write`，:407-414）。
- 列（:308-334）：`ID(面板打开时隐藏，桌面) / 角色名(tag「内置」warning /「自定义」info + 描述同行 :431-451) / 角色标识 / 状态(启用/停用) / 操作`；操作=「修改」「权限」「删除」（删除仅非内置，:461-496）。
- 底部（:498-503）：「内置角色由系统维护：不可删除，角色代码、启用状态与菜单权限均只读。」+「共 N 条」（前端分页，无后端窗口翻页）。
- 菜单权限面板（右侧 el-drawer/panel，:508-613）：头部「菜单权限（{name}）」（:538-540）；关闭 icon `aria-label="关闭菜单权限面板"`（:520）、保存 icon `aria-label="保存菜单权限"` + tippy 同文（:531-532）；内置角色 alert「内置角色的菜单权限由系统维护，此处只读查看。」（:548）；树搜索 placeholder「请输入菜单名称、权限码或路径进行搜索」（:554）；复选工具「展开/折叠」「全选/全不选」「父子联动」（tooltip「开启后勾选父级会联动勾选子级；后端按精确授权存储」，:568-569）；树节点 tag `目录/页面/权限` +「停用」+「已授权」（:584-607，类型文案 `filters.ts:7-11`）；底部提示「精确授权：默认不联动，勾选父级不会自动勾选子级；目录、页面与权限码需分别勾选。」（:611）。保存成功 ElMessage「角色“{n}”的权限已更新」（:269）。
- 新建/编辑对话框（title `新建角色`/`编辑角色`，:618）：角色名 placeholder「面向管理员的显示名称」（:625）、角色代码 placeholder「如 ops_admin，创建后不可修改」（:632，编辑 disabled）、描述 textarea、启用 switch + 内置角色灰字「内置角色的启用状态不可修改」（:647）。校验（:87-99）：「请填写角色名称」「以小写字母开头，仅含小写字母、数字、_ 或 -，长度 2-50」。成功「角色“{n}”已创建」（:142）/「角色信息已更新」（:149）。
- 删除确认：「确认删除角色“{n}”（{code}）？删除后无法恢复。」标题「删除角色」（:283-286）；成功「角色“{n}”已删除」（:298）。
- 失败兜底：「角色列表加载失败」(:57)/「创建角色失败」「更新角色失败」(:154)/「角色权限加载失败」(:238)/「保存角色权限失败」(:273)/「删除角色失败」(:302)。SSE/轮询：**无**。

## 15. 菜单管理 `#/system/menus`

`web/src/views/system/menus/index.vue`；API rbac.ts（GET `/menus/tree`、POST `/menus`、PUT/DELETE `/menus/{id}`）。权限：`menus:read`/`menus:write`（`routes_menus.py:24-25`）。admin 专属。

- 搜索（:341-349）：label「菜单名称：」placeholder「搜索标题 / 权限码 / 路径」——**前端过滤整棵树**（`filters.ts:55-66`），不发请求。
- PureTableBar 标题「菜单管理」（:366-371）；「新建菜单」（`v-perms menus:write`，:372-381）。
- 树形表（default-expand-all）列（:312-328）：`标题 / 类型(tag：目录=info、页面=primary、权限=warning) / 名称\权限码 / 路径 / 组件(空「—」) / 排序 / 显示(是/否) / 状态(启用/停用) / 操作`；操作：「修改」；「删除」或灰 tag「内置」（内置行无删除按钮，:432-459）。
- 底部（:461-463）：「内置菜单由系统维护，不能删除；名称、类型、上级、路径、组件不可修改，仅可调整标题、图标、排序与显示状态。」
- 新建/编辑对话框（title `新建菜单`/`编辑菜单`，:469）：编辑内置行时 alert「这是内置菜单：名称、类型、上级、路径与组件由系统固定，只能修改展示类字段。」（:474-481）；「类型」radio 目录/页面/权限（:483-493）；「上级」select placeholder 随类型「选择所属页面」/「选择所属目录」/「选择上级目录」，目录类型可选项含「根目录」（:100-102、:494-515）；「标题」placeholder「显示名称」；「名称/权限码」placeholder 随类型：权限→「如 jobs:read」，页面→「路由 name，如 Jobs」（:519-528）；「路径」placeholder「如 /logharvest/jobs」（:538）；「组件」placeholder「如 logharvest/jobs/index」（:550）；「图标」placeholder「如 ep/document」（:554）；「排序」input-number；switch「显示在菜单」「页面缓存」「启用」。
- 校验（:141-175）：「请填写标题」「请填写名称/权限码」「权限码需以小写字母开头，仅含小写字母、数字、: _ -」「页面/权限必须选择上级节点」「请填写路径」「请填写组件路径」。
- 成功 ElMessage：「菜单“{t}”已创建」（:262）/「已更新」（:267）/「已删除」（:302）。删除确认：「确认删除菜单“{t}”？其权限授权会被一并清理，且无法恢复。」标题「删除菜单」（:287-296）。
- 失败兜底：「菜单树加载失败」(:48)/「创建菜单失败」「更新菜单失败」(:275)/「删除菜单失败」(:305)。SSE/轮询：**无**。

## 16. 操作日志 `#/system/audit-logs`

`web/src/views/system/audit-logs/index.vue`；API GET `/operation-logs`（`web/src/api/audit.ts:9-10`）。权限：`audit:read`（`routes_operation_logs.py:20`）——菜单授予 admin/operator（`OPERATOR_MENUS` 含本页，`rbac_seed.py:248-272`），**viewer 无此菜单**（直达 = 404）。若接口仍返回 403（例如有菜单但被改权），整页降级 el-result：标题「无查看权限」，sub-title「操作日志需要 audit:read 权限（接口返回 403）。该权限默认只授予管理员与操作员，只读值守角色不可见。」（:191-196）。

- 筛选区（:203-298）：「时间范围：」el-date-picker type=datetimerange，start/end placeholder「开始时间」「结束时间」（:209-219）；「操作人：」select placeholder「操作人」，用户列表接口失败时降级 placeholder「用户列表不可用」且 disabled（:221-238、:49-60）；「动作族：」select placeholder「动作族」，选项 `auth. job. user. role. menu. rule_set. data_source. worker. system.`（:240-255；`auditTypes.ts:39-49`）；「对象类型：」选项 `job rule_set data_source user role menu worker system`（:257-273；`auditTypes.ts:52-61`）；「对象 ID：」输入 placeholder「对象 ID」（:274-279）；按钮「查询」（ri/search-line 图标，:286-292）「重置」（:293-296）。
- 时间范围不成对时前端拦截：ElMessage warning「时间范围必须成对给出，否则后端返回 422」（:68）。
- 灰字说明（:299-301）：「时间为浏览器本地时区显示；服务端按 UTC 存储，列表不含总数、按时间倒序」。
- PureTableBar 标题「操作日志」（:303）。列（:152-181）：`时间 / 操作人 / 动作 / 对象 / 摘要 / 来源 IP / 操作`。「操作人」是链接按钮：点击=就地按该人筛选并自动设最近 7 天范围（:122-128）；账号已删除显示「{name}（账号已删除）」（:331）；下方小字 role_hint。「对象」列 `{type}#{id}` 或「—」（:339-347）。行操作「详情」（:348-359）。窄屏裁「摘要」「来源 IP」（:175-178）。
- 空态 el-empty description = 加载错误文案 或「尚未产生写操作」（:361-365）；失败兜底「操作日志加载失败」（:90）。翻页：limit 50（:35），footer「第 N 页，本页 M 条」+ 上一页/下一页（:366-381）。
- 详情抽屉：el-drawer title「操作详情」（:385-390），size `min(560px, 92vw)`；descriptions `ID / 时间 / 操作人({actor}（{role_hint|无角色快照}）) / 动作 / 对象 / 摘要 / 来源 IP`（:392-417）；标题「detail（变更前后/参数摘要）」+ JSON `<pre>`（:418-424），无 detail 显示「该记录没有附加信息」（:425）。SSE/轮询：**无**。

## 17. E2E 断言关键文案清单

> 全部为源码逐字抄录；`{}` 为运行时插值。定位一律用文案本身（无 data-testid）。

**17.1 状态/标签配色映射**（`web/src/views/logharvest/format.ts`）：

| 语义 | 文本 | tag type | 证据 |
|---|---|---|---|
| 作业/文件任务状态 | `pending` | info（灰） | format.ts:81-92 |
| | `running` | primary（蓝） | :83 |
| | `succeeded` | success（绿） | :84 |
| | `partial` / `cancelled` | warning（黄） | :85-87 |
| | `failed` | danger（红） | :86 |
| 尝试历史 `timeout` | `timeout` | danger（特判，不在枚举映射内） | detail.vue:256-258 |
| 严重级 | `info`/`warning`/`error` | info/warning/danger | format.ts:94-98、115-117 |
| Worker | `online`/`busy`/`draining`/`offline` | success/primary/warning/info | format.ts:104-113 |
| 用户·角色·菜单状态 | 「启用」「停用」 | success / info | users:400-408、roles 列、menus 列 |
| 规则启用 | 「启用」「停用」 | success / info | edit-drawer.vue:143-147 |
| 菜单类型 | 「目录」「页面」「权限」 | info / primary / warning | menus 列 :312-328、filters.ts:7-11 |
| 角色来源 | 「内置」「自定义」 | warning / info | roles:431-451 |

空值统一「—」（UNKNOWN_TEXT，format.ts:14；时间列空为「-」:62）。

**17.2 全局/操作成功 ElMessage**（默认 **2000ms** 自动消失，`web/src/utils/message.ts:60`）：

| 文案 | 触发 | 证据 |
|---|---|---|
| 「登录成功」 | 登录 | login/index.vue:74（yaml:229） |
| 「作业 #N 已创建，共 M 个压缩文件」 | 向导提交 | create-dialog.vue:261-263 |
| 「作业 #N 当前状态：{status}」 | 列表取消 | jobs/index.vue:124 |
| 「已请求取消，作业状态：{status}」 | 详情取消 | detail.vue:365 |
| 「已重新排队 N 个失败文件」/「没有可重试的失败文件任务」(info)/「已重新排队 N 个失败文件，仍有 M 个待再次点击重试」(warning) | 重试失败文件 | detail.vue:385-393 |
| 「实时进度中断，已改为每 5 秒轮询作业详情」(warning)/「实时进度连接已结束，但作业尚未进入终态，已改为每 5 秒轮询」 | SSE 降级 | detail.vue:347、:337-339 |
| 「维护完成：回收 X 个文件任务，降级 Y 台 Worker，收尾 Z 个作业」 | 概览执行维护 | overview/index.vue:153-156 |
| 「数据源“{name}”已创建」 | 新建数据源 | datasources:106 |
| 「{id}：drain_requested={bool}，状态 {status}」/「{id} 已恢复领取（状态 {status}）」/「版本 {v}：已排空 {n} 台，标记 {m} 台离线（重启后生效）」 | Worker 排空/恢复/按版本排空 | workers:117-120、:133-136、:167-169 |
| 「请先填写要排空的 Worker 版本号」(warning) | 空版本排空 | workers:150-153 |
| 「已创建版本 {n}」 | 规则另存新版本 | maintenance/index.vue:151 |
| 「用户“{u}”已创建」「用户信息已更新」「用户“{u}”的角色已更新」「用户“{u}”的密码已重置」「用户“{u}”已删除」 | 用户 CRUD | users:171、:177、:208-210、:263-265、:293 |
| 「角色“{n}”已创建」「角色信息已更新」「角色“{n}”的权限已更新」「角色“{n}”已删除」 | 角色 CRUD | roles:142、:149、:269、:298 |
| 「菜单“{t}”已创建」「菜单“{t}”已更新」「菜单“{t}”已删除」 | 菜单 CRUD | menus:262、:267、:302 |

**17.3 确认框（ElMessageBox，`.el-message-box`）**：

| 正文 | 标题 | 按钮 | 证据 |
|---|---|---|---|
| 「确认请求取消作业 #N（名称）？已进入运行中的文件任务会跑完后收尾。」 | 取消作业 | 「确认取消」「再想想」 | jobs/index.vue:110-118 |
| 「确认请求取消该作业？」 | 取消作业 | 默认 确定/取消 | detail.vue:356 |
| 「确认重试该作业全部失败的文件任务？失败任务会重置为待处理并重新排队。」 | 重试失败文件 | 默认 | detail.vue:374-378 |
| 「确认排空 Worker {id}？它跑完当前文件任务后不再领取新任务。」 | 排空 Worker | 「排空」「取消」 | workers:104-110 |
| 「确认排空版本 {v} 的全部 Worker？离线机器也会被记下标记，重启后生效。」 | 按版本排空 | 「排空」「取消」 | workers:155-160 |
| 「确认删除规则“{id|序号}”？保存新版本后才会落库。」 | 删除规则 | 「删除」「取消」 | rule-table.vue:48-60 |
| 「确认删除用户“{u}”？删除后无法恢复。」 | 删除用户 | 「删除」「取消」 | users:276-287 |
| 「确认删除角色“{n}”（{code}）？删除后无法恢复。」 | 删除角色 | 默认 danger | roles:283-286 |
| 「确认删除菜单“{t}”？其权限授权会被一并清理，且无法恢复。」 | 删除菜单 | 默认 | menus:287-296 |

**17.4 加载/操作失败兜底**（apiErrorDetail/formatDetailError，有后端 detail 时优先显示 detail）：

「登录失败」；「规则集列表加载失败」（rulesets:47、maintenance:69）；「规则集详情加载失败」（edit-drawer:69）；「规则集版本加载失败」（maintenance:99）；「规则检索失败」（maintenance:195）；「保存失败」（maintenance:157）；「作业下拉加载失败，可改用时间范围查询」(warning)（matches:139-150）；「尝试历史加载失败：以下是接口错误，不代表「该任务没有尝试记录」」（detail.vue:610-619）；「用户列表加载失败」「角色列表加载失败」「创建用户失败」「更新用户失败」「分配角色失败」「重置密码失败」「删除用户失败」；「角色权限加载失败」「保存角色权限失败」「删除角色失败」；「菜单树加载失败」「创建菜单失败」「更新菜单失败」「删除菜单失败」；「操作日志加载失败」；「请求失败」（formatDetailError 默认 fallback，format.ts:147）。

**17.5 表单校验红字（`.el-form-item__error`）与页内错误块**：见 §13「请填写用户名」等、§14「请填写角色名称」等、§15「请填写标题」等、§12 rule-form.ts 清单、§5 向导「请填写作业名称/请选择数据源/请填写要扫描的目录/请选择规则集版本」、§8「请填写数据源名称/请填写挂载点绝对路径」、§1「请输入账号/请输入密码」。后端错误页内展示：向导「提交未成功」alert（400「路径不合法：」/409「幂等冲突或目录不可用：…」/422「参数校验失败：」/503「目录扫描超过 10 秒：」，create-dialog.vue:232-239）；规则维护 alert `<pre>`；抽屉「详情加载失败」alert。

**17.6 整页降级/权限文案**：404「抱歉，你访问的页面不存在」（views/error/404.vue:48）；403「抱歉，你无权访问该页面」（views/error/403.vue:48）；统计页 el-result「无查看权限 / 任务统计需要 jobs:read 权限（接口返回 403），请联系管理员分配后重试。」（stats:167-172）；操作日志页 el-result（§16）。

## 18. Playwright 落地注意

- **hash 路由 goto**：`page.goto('http://127.0.0.1:18080/#/logharvest/jobs')`。已在本页时再 goto 只有 hash 不同**不会 reload**（同一文档），跨页导航用点击或 `page.waitForURL(/#\/logharvest\/jobs$/)`；要强制重进（重拉 /auth/routes）用 `page.reload()`。登录后落地：admin/operator → `#/home`。
- **登录态**：cookie `authorized-token`+`multiple-tabs`、localStorage `user-info`（utils/auth.ts:23-30）；`storageState` 两者都要带上；清理=清 cookie+localStorage。三账号（admin/operator/viewer）建议各存一份 state 文件。
- **无 data-testid、文案即选择器**：`getByRole('button', { name: '新建任务' })`、`getByPlaceholder('关键字（本页）')` 优先；同名按钮用行作用域 `page.locator('.el-table__row', { hasText: '…' }).getByRole('button', { name: '详情' })`。
- **v-perms 是 removeChild**（`web/src/directives/perms/index.ts`：无权限时元素**不在 DOM**）：负断言用 `await expect(locator).toHaveCount(0)`，不能用 `toBeVisible({visible:false})` 之类语义混淆。
- **el-select**：下拉面板挂在 body 的 `.el-select-dropdown`，**已关闭的旧面板仍留在 DOM**——必须 `page.locator('.el-select-dropdown:visible')` 或先 `getByRole('combobox')` click 再在可见 popper 内 `getByText('running', {exact:true})`；选项文本多为英文码原文。登录页「天内免登录」是**原生 select**，不是 el-select。
- **el-dialog / el-message-box / el-drawer**：均 `append-to-body`，一次只应有一个可见；用 `getByRole('dialog')`/`.el-message-box`/`.el-drawer` 加 `:visible` 过滤。`:close-on-click-modal="false"`——点遮罩不会关窗，关窗要点「取消/关闭/确定」或 header 的 `.el-dialog__headerbtn`。确认框按钮文案各页自定义（§17.3），不要假设「确定」。
- **el-table 单元格**：表头 `.el-table__header th >> text=列名`；数据 `page.locator('.el-table__body .el-table__row')`；`showOverflowTooltip` 列文本仍在单元格内（tooltip 是 body 级 popper，不必追）；`fixed: right` 操作列在 EP 里是**克隆表**——行按钮直接在克隆侧可见，用 getByRole 即可，但避免 `.el-table__body` first() 硬索引。@pureadmin/table 的 slot 坑（§0）意味着纯文本列没有包装元素。
- **ElMessage 只活 2000ms**（`web/src/utils/message.ts:60` 覆盖了 EP 默认 3000）：断言写法 `await expect(page.locator('.el-message')).toContainText('已创建版本', { timeout: 1500 })`，或在触发点击**之前** `page.waitForSelector('.el-message')`；慢断言会漏。多条消息会叠（`.el-message` first()）。
- **el-date-picker**：`fill('2026-10-01 00:00:00')` 后必须 `press('Enter')` 才提交模型；datetimerange（操作日志）是一个 input 两个 placeholder（开始时间/结束时间），fill 后点面板「确定」或 Enter；matches 页是两个独立 picker（placeholder 是英文 `created_from`/`created_to`）。注意提交给后端带 `Z`（format.ts toApiDateTime），断言请求用 `page.waitForRequest`。
- **PureTableBar 图标按钮无文字**：每表右上「刷新」（tippy「刷新」）、「密度」（选项 宽松/默认/紧凑）、「列设置」（popover 含「列展示」「重置」）、「展开/折叠」全部只有图标（`web/src/components/RePureTableBar/src/bar.tsx:305-379`、locale zh-CN.yaml:249-262），tooltip 文案在 body popper。定位刷新可用 `.el-icon` 容器顺序或给表条加 role 后 nth-child；标题文本在 `<p class="font-bold truncate">`，用 `getByText('分析作业')` 可作行/表锚点。
- **SSE 长连接**：作业详情页 `onMounted` 即打开 `GET /api/jobs/{id}/events`（fetch+ReadableStream，带 Bearer），该请求**永不自然结束**（非终态）——`waitForLoadState('networkidle')` 在此页不可用；mock 进度用 `page.route('**/jobs/*/events', …)` fulfill 自定义 SSE body 或直接 abort 触发「轮询降级」文案断言。其余页面均无长连接。
- **裸数组分页断言**：无 total，列表页 footer 形如「第 N 页，本页 M 条」；「下一页」enabled 的唯一条件是本页取满 limit（jobs=50、rulesets/users=20、workers=100、audit=50、datasources=50、matches 只有「下一页」游标式）——构造数据要让行数跨过 limit 才能测翻页。
- **视口固定 ≥1280**：多处列/按钮随 `appStore.device` 收窄（§0 窄屏差异），否则断言列集合不一致。
- **404≠403**：越权直达动态页得到的是 404 全屏页（§2 修订），断言「抱歉，你访问的页面不存在」；只有静态 `/access-denied` 场景（无任何角色）才是 403 文案。
- **el-tabs 切换**（规则维护）：Tab 头 `getByRole('tab', { name: '跨版本检索' })`；「查看规则/编辑规则」Tab 名随角色变，选择器别写死。
- **请求断言优于 DOM 轮询**：列表刷新类操作可 `Promise.all([page.waitForResponse(/\/api\/(jobs|rule-sets|operation-logs)\?/), click()])`，避免被 2s 消息时限坑。

<!-- SECTION-END -->
