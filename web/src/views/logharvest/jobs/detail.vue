<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import {
  apiErrorDetail,
  cancelJob,
  getJob,
  listJobFiles,
  retryFailedFiles
} from "@/api/logharvest";
import {
  JOB_STATUS_OPTIONS,
  TERMINAL_JOB_STATUSES,
  type FileTask,
  type Job,
  type ProgressEvent
} from "@/api/logharvestTypes";
import { streamJobProgress } from "@/utils/sse";
import { formatGB, formatDateTime, statusTagType } from "../format";

defineOptions({ name: "JobDetail" });

const route = useRoute();
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

const filteredFiles = computed(() =>
  statusFilter.value
    ? files.value.filter(item => item.status === statusFilter.value)
    : files.value
);

const hasNext = computed(() => files.value.length === page.limit);

/** 失败文件数：优先用 SSE 快照的全量计数，退化时只看当前页 */
const failedCount = computed(
  () =>
    progress.value?.by_status?.failed ??
    files.value.filter(item => item.status === "failed").length
);

const columns: TableColumnList = [
  { label: "ID", prop: "id", width: 90 },
  { label: "文件", prop: "file_name", minWidth: 220, showOverflowTooltip: true },
  { label: "状态", prop: "status", width: 110 },
  { label: "大小", prop: "size_bytes", width: 120 },
  { label: "尝试", prop: "attempts", width: 90 },
  { label: "Worker", prop: "worker_id", width: 160, showOverflowTooltip: true },
  { label: "已处理行", prop: "processed_lines", width: 120 },
  { label: "命中行", prop: "matched_rows", width: 110 },
  { label: "心跳", prop: "heartbeat_at", width: 180 },
  { label: "错误", prop: "error", minWidth: 180, showOverflowTooltip: true }
];

async function loadJob() {
  job.value = await getJob(jobId);
}

async function loadFiles() {
  try {
    files.value = await listJobFiles(jobId, {
      limit: page.limit,
      offset: page.offset
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
    await streamJobProgress(
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
    // 流正常结束（作业已终态或已被删除），后端发完最后一个快照就关流
    if (streaming.value) {
      await refreshJobAndFiles();
      stopAll();
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
    const { retried } = await retryFailedFiles(jobId);
    message(`已重新排队 ${retried} 个失败文件`, { type: "success" });
    await Promise.all([loadJob(), loadFiles()]);
    // 重试把作业从终态拉回 pending，作业仍非终态时恢复实时进度
    if (!terminal.value && !streaming.value && !pollingFallback.value) {
      startStream();
    }
  } catch (error) {
    message(apiErrorDetail(error, "重试失败"), { type: "error" });
  }
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
  if (!terminal.value) startStream();
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
          <div class="flex items-center gap-2">
            <el-tag v-if="job" :type="statusTagType(job.status)" effect="light">
              {{ job.status }}
            </el-tag>
            <el-tag v-if="streaming" type="success" effect="plain">实时进度</el-tag>
            <el-tag v-else-if="pollingFallback" type="warning" effect="plain">
              轮询降级
            </el-tag>
            <el-button
              v-if="job && !terminal"
              type="danger"
              plain
              @click="onCancel"
            >
              取消作业
            </el-button>
            <el-button
              v-if="failedCount > 0"
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
            :data="filteredFiles"
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
              后端该接口无 status 过滤参数（筛选只作用于本页）；失败文件可用上方
              “重试失败文件”重新排队（{{ failedCount }} 个失败）
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
  </div>
</template>
