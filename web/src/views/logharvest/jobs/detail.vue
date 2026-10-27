<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import {
  apiErrorDetail,
  apiErrorStatus,
  cancelJob,
  getJob,
  listJobFiles,
  listTaskAttempts,
  retryFailedFiles
} from "@/api/logharvest";
import {
  JOB_STATUS_OPTIONS,
  TERMINAL_JOB_STATUSES,
  type FileTask,
  type FileTaskAttempt,
  type Job,
  type ProgressEvent
} from "@/api/logharvestTypes";
import { streamJobProgress } from "@/utils/sse";
import {
  UNKNOWN_TEXT,
  countOrUnknown,
  formatDateTime,
  formatGB,
  statusTagType,
  textOrUnknown
} from "../format";

defineOptions({ name: "JobDetail" });

const route = useRoute();
const router = useRouter();
const jobId = Number(route.params.id);

const job = ref<Job | null>(null);
const files = ref<FileTask[]>([]);
const progress = ref<ProgressEvent | null>(null);
const loading = ref(true);
const streaming = ref(false);
const pollingFallback = ref(false);

const page = reactive({ limit: 50, offset: 0 });
const statusFilter = ref<string>("");

let controller: AbortController | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

const terminal = computed(() =>
  job.value ? TERMINAL_JOB_STATUSES.includes(job.value.status) : false
);

/** 进度：已终态的文件任务数 / 总数（SSE 快照里的 by_status） */
const doneCount = computed(() => {
  const byStatus = progress.value?.by_status ?? {};
  return TERMINAL_JOB_STATUSES.reduce(
    (sum, status) => sum + (byStatus[status] ?? 0),
    0
  );
});

const percent = computed(() => {
  const total = progress.value?.total ?? job.value?.total_files ?? 0;
  if (!total) return terminal.value ? 100 : 0;
  return Math.min(100, Math.round((doneCount.value / total) * 100));
});

/** 状态筛选已下推给后端：切换时回到第一页重新拉取。 */
function onStatusFilterChange() {
  page.offset = 0;
  loadFiles();
}

const hasNext = computed(() => files.value.length === page.limit);

/** 失败文件数：优先用 SSE 快照的全量计数，退化时只看当前页 */
const failedCount = computed(
  () =>
    progress.value?.by_status?.failed ??
    files.value.filter(item => item.status === "failed").length
);

/**
 * `@pureadmin/table` 3.3.0 只按列定义里的 `slot` 字段取内容插槽（源码
 * `dist/index.es.js`：`const {slot: s, prop: E} = n; ... s ? t[s]?.() : void 0`），
 * **没有 `slot` 时不会回退到与 `prop` 同名的插槽**——插槽模板会被整体忽略，单元格退回成
 * 裸的 `row[prop]` 文本。这里必须逐列写 `slot`，否则「历史」按钮根本不会出现在表格里。
 */
const columns: TableColumnList = [
  { label: "ID", prop: "id", width: 90 },
  { label: "文件", prop: "file_name", minWidth: 220, showOverflowTooltip: true },
  { label: "状态", prop: "status", slot: "status", width: 110 },
  { label: "大小", prop: "size_bytes", slot: "size_bytes", width: 120 },
  { label: "尝试", prop: "attempts", slot: "attempts", width: 90 },
  { label: "历史", prop: "history", slot: "history", width: 90 },
  {
    label: "Worker",
    prop: "worker_id",
    slot: "worker_id",
    width: 160,
    showOverflowTooltip: true
  },
  { label: "已处理行", prop: "processed_lines", width: 120 },
  { label: "命中行", prop: "matched_rows", width: 110 },
  {
    label: "心跳",
    prop: "heartbeat_at",
    slot: "heartbeat_at",
    width: 180
  },
  {
    label: "错误",
    prop: "error",
    slot: "error",
    minWidth: 180,
    showOverflowTooltip: true
  }
];

/**
 * 尝试历史抽屉的状态。
 *
 * `error` 与 `rows` 是两种必须分开的状态：
 * - `rows = []` 且 `loaded = true` 且 `error = ""` → 后端给了 `200 []`，即「该任务一次都没跑过」
 * - `error` 非空 → 请求失败（含 404：后端还没有该端点，或任务不属于该作业），
 *   此时**不显示空态**，否则「加载失败」会被读成「没有重试过」
 */
const attempts = reactive({
  visible: false,
  loading: false,
  loaded: false,
  error: "",
  taskId: null as number | null,
  fileName: "",
  rows: [] as FileTaskAttempt[],
  /** 递增的请求号：快速切换任务时丢弃过期响应，避免把上一个任务的历史画到当前任务上 */
  requestId: 0
});

const attemptColumns: TableColumnList = [
  { label: "次序", prop: "attempt_no", slot: "attempt_no", width: 70 },
  {
    label: "Worker",
    prop: "worker_id",
    slot: "worker_id",
    minWidth: 150,
    showOverflowTooltip: true
  },
  {
    label: "版本",
    prop: "worker_version",
    slot: "worker_version",
    width: 110,
    showOverflowTooltip: true
  },
  { label: "状态", prop: "status", slot: "status", width: 100 },
  { label: "开始时间", prop: "started_at", slot: "started_at", width: 170 },
  { label: "结束时间", prop: "finished_at", slot: "finished_at", width: 170 },
  {
    label: "已处理行",
    prop: "processed_lines",
    slot: "processed_lines",
    width: 110
  },
  { label: "命中行", prop: "matched_rows", slot: "matched_rows", width: 100 },
  {
    label: "错误",
    prop: "error",
    slot: "error",
    minWidth: 220,
    showOverflowTooltip: true
  }
];

function attemptErrorText(error: unknown): string {
  const status = apiErrorStatus(error);
  const detail = apiErrorDetail(error, "请求失败");
  if (status === undefined) {
    return `请求没有收到响应（网络中断、后端未启动或超时）。${detail}`;
  }
  if (status === 404) {
    return `HTTP 404：${detail}。两种可能——后端还没有该端点（尝试历史接口由 M2c Task 3 提供），或文件任务 #${attempts.taskId} 不属于作业 #${jobId}。`;
  }
  return `HTTP ${status}：${detail}`;
}

async function loadAttempts() {
  const taskId = attempts.taskId;
  if (taskId === null) return;
  const request = ++attempts.requestId;
  attempts.loading = true;
  attempts.error = "";
  try {
    const rows = await listTaskAttempts(jobId, taskId);
    if (request !== attempts.requestId) return;
    if (!Array.isArray(rows)) {
      // 契约是裸数组；形状不对就如实报错，绝不退化成「该任务没有尝试记录」
      attempts.rows = [];
      attempts.loaded = false;
      attempts.error = `接口返回的不是数组（实际类型 ${typeof rows}）：后端未按 list[FileTaskAttemptRead] 契约实现。`;
      message(attempts.error, { type: "error" });
      return;
    }
    attempts.rows = rows;
    attempts.loaded = true;
  } catch (error) {
    if (request !== attempts.requestId) return;
    // 失败：清空数据但保留 error，让界面走「错误」分支而不是「暂无记录」分支
    attempts.rows = [];
    attempts.loaded = false;
    attempts.error = attemptErrorText(error);
    message(attempts.error, { type: "error" });
  } finally {
    if (request === attempts.requestId) attempts.loading = false;
  }
}

/** 打开某个文件任务的尝试历史抽屉 */
function openAttemptsFor(taskId: number, fileName: string) {
  attempts.visible = true;
  attempts.taskId = taskId;
  attempts.fileName = fileName;
  attempts.rows = [];
  attempts.error = "";
  attempts.loaded = false;
  void loadAttempts();
}

/** 表格行点「历史」：记下 job/task，切换任务时历史重新拉取 */
function openAttempts(row: FileTask) {
  openAttemptsFor(row.id, row.file_name);
}

/**
 * 命中结果页的「所属文件」链接带 `?file_task=` 跳回来。
 * 该任务可能不在当前这一窗口的文件列表里（列表只有 limit/offset），
 * 但尝试历史接口按 `task_id` 查，所以照常打开，只是标题里带不出文件名。
 */
function openAttemptFromQuery() {
  const raw = typeof route.query.file_task === "string" ? route.query.file_task : "";
  const taskId = Number(raw);
  if (!Number.isInteger(taskId) || taskId <= 0) return;
  openAttemptsFor(taskId, files.value.find(item => item.id === taskId)?.file_name ?? "");
}

function closeAttempts() {
  attempts.visible = false;
  // 让仍在飞行中的响应作废（抽屉已关，不该再改状态或弹提示）
  attempts.requestId += 1;
}

/** 一次尝试的历史状态：`timeout`（失联回收写入）不在作业状态枚举里，配色单独兜底 */
function attemptStatusTagType(status: string) {
  return status === "timeout" ? "danger" : statusTagType(status);
}

async function loadJob() {
  job.value = await getJob(jobId);
}

async function loadFiles() {
  try {
    // 状态筛选下推给后端（?status=），不再只在当前页里过滤，
    // 否则第 2 页之后的匹配行永远看不到。
    files.value = await listJobFiles(jobId, {
      limit: page.limit,
      offset: page.offset,
      ...(statusFilter.value ? { status: statusFilter.value } : {})
    });
  } catch (error) {
    files.value = [];
    message(apiErrorDetail(error, "文件任务列表加载失败"), { type: "error" });
  }
}

function stopAll() {
  controller?.abort();
  controller = null;
  streaming.value = false;
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** 降级：SSE 断开或浏览器不支持流式响应时，每 5 秒轮询一次作业详情 */
function startPolling() {
  if (pollTimer) return;
  pollingFallback.value = true;
  pollTimer = setInterval(async () => {
    try {
      await loadJob();
      await loadFiles();
    } catch {
      /* 轮询期间的错误由下一次刷新覆盖，不重复弹提示 */
    }
    if (terminal.value) {
      stopAll();
      pollingFallback.value = false;
    }
  }, 5000);
}

async function refreshJobAndFiles() {
  await Promise.all([loadJob().catch(() => {}), loadFiles()]);
}

async function startStream() {
  controller = new AbortController();
  streaming.value = true;
  try {
    const last = await streamJobProgress(
      jobId,
      event => {
        progress.value = event;
        if (job.value) job.value.status = event.status;
        // 快照只带计数，终态时的时间/错误字段还是要从作业详情取
        if (TERMINAL_JOB_STATUSES.includes(event.status)) {
          void refreshJobAndFiles().then(stopAll);
        }
      },
      controller.signal
    );
    // 流结束但收尾路径没跑（例如作业被删除、代理中途切断）：
    // 只有看到终态快照才算"作业已完成"，否则必须继续跟进，
    // 不然界面会停在最后一次非终态快照上假装还在进行中却再也不刷新。
    if (streaming.value) {
      const endedTerminal =
        last !== undefined && TERMINAL_JOB_STATUSES.includes(last.status);
      await refreshJobAndFiles();
      stopAll();
      if (!endedTerminal) {
        message("实时进度连接已结束，但作业尚未进入终态，已改为每 5 秒轮询", {
          type: "warning"
        });
        startPolling();
      }
    }
  } catch (error) {
    // onBeforeUnmount / 终态收尾都会 abort，这是正常路径
    if ((error as Error)?.name === "AbortError") return;
    stopAll();
    message("实时进度中断，已改为每 5 秒轮询作业详情", { type: "warning" });
    startPolling();
  } finally {
    loading.value = false;
  }
}

async function onCancel() {
  try {
    await ElMessageBox.confirm("确认请求取消该作业？", "取消作业", {
      type: "warning"
    });
  } catch {
    return;
  }
  try {
    const updated = await cancelJob(jobId);
    job.value = updated;
    message(`已请求取消，作业状态：${updated.status}`, { type: "success" });
    await loadFiles();
  } catch (error) {
    message(apiErrorDetail(error, "取消失败"), { type: "error" });
  }
}

async function onRetryFailed() {
  try {
    await ElMessageBox.confirm(
      "确认重试该作业全部失败的文件任务？失败任务会重置为待处理并重新排队。",
      "重试失败文件",
      { type: "warning" }
    );
  } catch {
    return;
  }
  try {
    const { retried, remaining_failed: remaining } = await retryFailedFiles(jobId);
    // 后端单次只重置 200 个任务，剩下的要如实说出来，否则一次点击会被当成"全部重试完了"
    let summary = `已重新排队 ${retried} 个失败文件`;
    let level: "success" | "warning" | "info" = "success";
    if (retried === 0) {
      summary = "没有可重试的失败文件任务";
      level = "info";
    } else if (remaining > 0) {
      summary = `已重新排队 ${retried} 个失败文件，仍有 ${remaining} 个待再次点击重试`;
      level = "warning";
    }
    message(summary, { type: level });
    await Promise.all([loadJob(), loadFiles()]);
    // 重试把作业从终态拉回 running，作业仍非终态时恢复实时进度
    if (!terminal.value && !streaming.value && !pollingFallback.value) {
      startStream();
    }
  } catch (error) {
    message(apiErrorDetail(error, "重试失败"), { type: "error" });
  }
}

/** 「查看命中结果」：带上 job_id，日志分析页会直接按本作业查询 */
function goMatches() {
  router.push({ path: "/logharvest/matches", query: { job_id: String(jobId) } });
}

onMounted(async () => {
  if (!Number.isFinite(jobId)) {
    message("作业 ID 不合法", { type: "error" });
    loading.value = false;
    return;
  }
  try {
    await Promise.all([loadJob(), loadFiles()]);
  } catch (error) {
    message(apiErrorDetail(error, "作业加载失败"), { type: "error" });
    loading.value = false;
    return;
  }
  loading.value = false;
  openAttemptFromQuery();
  // 终态作业也打开 SSE：服务端会立即发一个终态快照并结束，头部进度/行数才不会停在 0。
  startStream();
});

onBeforeUnmount(stopAll);
</script>

<template>
  <div class="p-4">
    <el-card v-loading="loading" shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-medium">
            作业 #{{ job?.id }} · {{ job?.name ?? "加载中" }}
          </span>
          <div class="flex flex-wrap items-center justify-end gap-2">
            <el-tag v-if="job" :type="statusTagType(job.status)" effect="light">
              {{ job.status }}
            </el-tag>
            <el-tag v-if="streaming" type="success" effect="plain">实时进度</el-tag>
            <el-tag v-else-if="pollingFallback" type="warning" effect="plain">
              轮询降级
            </el-tag>
            <el-button type="primary" plain @click="goMatches">
              查看命中结果
            </el-button>
            <el-button
              v-if="job && !terminal"
              v-perms="'jobs:operate'"
              type="danger"
              plain
              @click="onCancel"
            >
              取消作业
            </el-button>
            <el-button
              v-if="failedCount > 0"
              v-perms="'jobs:operate'"
              type="warning"
              plain
              @click="onRetryFailed"
            >
              重试失败文件（{{ failedCount }}）
            </el-button>
          </div>
        </div>
      </template>

      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="数据源">#{{ job?.data_source_id }}</el-descriptions-item>
        <el-descriptions-item label="规则集">#{{ job?.rule_set_id }}</el-descriptions-item>
        <el-descriptions-item label="来源目录">
          {{ job?.source_path ?? "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="文件数">{{ job?.total_files ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="总字节">
          {{ formatGB(job?.total_bytes) }}
        </el-descriptions-item>
        <el-descriptions-item label="请求取消">
          {{ job?.cancel_requested ? "是" : "否" }}
        </el-descriptions-item>
        <el-descriptions-item label="开始时间">
          {{ formatDateTime(job?.started_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="结束时间">
          {{ formatDateTime(job?.finished_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="创建人">{{ job?.created_by ?? "-" }}</el-descriptions-item>
        <el-descriptions-item label="错误" :span="3">
          {{ job?.error ?? "-" }}
        </el-descriptions-item>
      </el-descriptions>

      <div class="mt-4">
        <div class="mb-1 text-sm text-gray-600 dark:text-gray-400">
          进度 {{ doneCount }} / {{ progress?.total ?? job?.total_files ?? 0 }} ·
          已处理行数 {{ progress?.processed_lines ?? 0 }} · 命中行数
          {{ progress?.matched_rows ?? 0 }}
        </div>
        <el-progress :percentage="percent" :stroke-width="14" striped striped-flow />
        <div v-if="progress" class="mt-2 flex flex-wrap gap-2">
          <el-tag
            v-for="(count, status) in progress.by_status"
            :key="status"
            :type="statusTagType(String(status))"
            effect="plain"
          >
            {{ status }}: {{ count }}
          </el-tag>
        </div>
      </div>
    </el-card>

    <div class="mt-4">
      <PureTableBar title="文件任务" :columns="columns" @refresh="loadFiles">
        <template #buttons>
          <el-select
            v-model="statusFilter"
            clearable
            placeholder="状态筛选"
            class="w-36"
            @change="onStatusFilterChange"
          >
            <el-option
              v-for="item in JOB_STATUS_OPTIONS"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
        </template>
        <template v-slot="{ size }">
          <pure-table
            :size="size"
            row-key="id"
            adaptive
            :data="files"
            :columns="columns"
          >
            <template #status="{ row }">
              <el-tag :type="statusTagType(row.status)" :size="size" effect="light">
                {{ row.status }}
              </el-tag>
            </template>
            <template #size_bytes="{ row }">
              {{ formatGB(row.size_bytes) }}
            </template>
            <template #attempts="{ row }">
              {{ row.attempts }} / {{ row.max_attempts }}
            </template>
            <template #history="{ row }">
              <el-button :size="size" link type="primary" @click="openAttempts(row)">
                历史
              </el-button>
            </template>
            <template #worker_id="{ row }">
              {{ row.worker_id ?? "-" }}
            </template>
            <template #heartbeat_at="{ row }">
              {{ formatDateTime(row.heartbeat_at) }}
            </template>
            <template #error="{ row }">
              {{ row.error ?? "-" }}
            </template>
          </pure-table>
          <div class="mt-3 flex items-center justify-between text-sm">
            <span class="text-gray-500">
              状态筛选已下推到后端（<code>?status=</code>，非法值返回
              422）；接口无总数，所以仍是窗口式翻页。失败文件可用上方
              “重试失败文件”重新排队（{{ failedCount }} 个失败）；“历史”打开该任务的尝试记录
            </span>
            <div class="flex gap-2">
              <el-button :disabled="page.offset === 0" @click="(page.offset -= page.limit), loadFiles()">
                上一页
              </el-button>
              <el-button :disabled="!hasNext" @click="(page.offset += page.limit), loadFiles()">
                下一页
              </el-button>
            </div>
          </div>
        </template>
      </PureTableBar>
    </div>

    <el-drawer
      v-model="attempts.visible"
      size="920px"
      :title="
        `尝试历史 · 文件任务 #${attempts.taskId ?? '-'}${
          attempts.fileName ? `（${attempts.fileName}）` : ''
        }`
      "
      @close="closeAttempts"
    >
      <div v-loading="attempts.loading" style="min-height: 8rem">
        <p class="mb-3 text-xs text-gray-500">
          作业 #{{ jobId }}。一行 = Worker 的一次领取：领取时写
          <code>running</code>，结束时改写为
          <code>succeeded</code> / <code>failed</code> /
          <code>cancelled</code>，Worker 失联被维护任务回收时写
          <code>timeout</code>。<br />
          <b>“{{ UNKNOWN_TEXT }}” 表示该列没有值</b>（例如仍在执行的尝试没有结束时间、
          成功的一次没有错误文案），不会用 0 或空文案顶替。
        </p>

        <el-alert
          v-if="attempts.error"
          type="error"
          :closable="false"
          show-icon
          class="mb-3"
          title="尝试历史加载失败：以下是接口错误，不代表「该任务没有尝试记录」"
        >
          <pre class="whitespace-pre-wrap">{{ attempts.error }}</pre>
        </el-alert>

        <pure-table
          v-else-if="attempts.rows.length > 0"
          size="small"
          row-key="attempt_no"
          adaptive
          :border="true"
          :data="attempts.rows"
          :columns="attemptColumns"
        >
          <template #attempt_no="{ row }">
            {{ countOrUnknown(row.attempt_no) }}
          </template>
          <template #status="{ row }">
            <el-tag
              :type="attemptStatusTagType(row.status)"
              size="small"
              effect="light"
            >
              {{ row.status }}
            </el-tag>
          </template>
          <template #worker_id="{ row }">
            {{ textOrUnknown(row.worker_id) }}
          </template>
          <template #worker_version="{ row }">
            {{ textOrUnknown(row.worker_version) }}
          </template>
          <template #started_at="{ row }">
            {{ row.started_at ? formatDateTime(row.started_at) : UNKNOWN_TEXT }}
          </template>
          <template #finished_at="{ row }">
            {{ row.finished_at ? formatDateTime(row.finished_at) : UNKNOWN_TEXT }}
          </template>
          <template #processed_lines="{ row }">
            {{ countOrUnknown(row.processed_lines) }}
          </template>
          <template #matched_rows="{ row }">
            {{ countOrUnknown(row.matched_rows) }}
          </template>
          <template #error="{ row }">
            <span v-if="row.error" class="text-red-500">{{ row.error }}</span>
            <span v-else>{{ UNKNOWN_TEXT }}</span>
          </template>
        </pure-table>

        <el-empty
          v-else-if="attempts.loaded && !attempts.loading"
          description="该任务尚无尝试记录（接口返回 200 空数组，未开始执行）"
          :image-size="64"
        />
      </div>
      <template #footer>
        <div class="flex items-center justify-between gap-3">
          <span class="text-xs text-gray-500">
            {{
              attempts.error
                ? "接口没有返回数据，表格已隐藏（不显示为空表）"
                : `已加载 ${attempts.rows.length} 次尝试，后端按 attempt_no 升序返回`
            }}
          </span>
          <el-button
            :loading="attempts.loading"
            :disabled="attempts.taskId === null"
            @click="loadAttempts"
          >
            重新加载
          </el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>
