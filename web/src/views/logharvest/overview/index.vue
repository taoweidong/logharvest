<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import {
  apiErrorDetail,
  overview as fetchOverview,
  runMaintenance,
  systemStatus
} from "@/api/logharvest";
import {
  JOB_STATUS_OPTIONS,
  type MaintenanceResult,
  type Overview,
  type SystemStatus,
  type TaskStatus
} from "@/api/logharvestTypes";
import { formatDuration, statusTagType } from "../format";

defineOptions({ name: "Overview" });

/** 最老 pending 超过这个秒数就高亮（队列积压预警） */
const PENDING_WARN_SECONDS = 300;

const loading = ref(false);
const data = ref<Overview | null>(null);
/**
 * `/api/system/overview` 失败时退回后端已有的 `/api/system/status`
 * （只有文件任务计数、最老 pending 秒数、存活 Worker 数），页面不留空。
 */
const fallback = ref<SystemStatus | null>(null);
const degraded = ref(false);
const loadError = ref("");
const maintaining = ref(false);
const maintenanceResult = ref<MaintenanceResult | null>(null);
const loadedAt = ref("");

const TASK_STATUS_OPTIONS: TaskStatus[] = [
  "pending",
  "running",
  "succeeded",
  "partial",
  "failed",
  "cancelled"
];

function sum(counts: Record<string, number | undefined> | undefined): number {
  if (!counts) return 0;
  return Object.values(counts).reduce(
    (total, value) => total + (value ?? 0),
    0
  );
}

const fileTasks = computed<Record<string, number | undefined>>(
  () => data.value?.file_tasks ?? fallback.value?.file_tasks ?? {}
);

const jobTasks = computed<Record<string, number | undefined>>(
  () => data.value?.jobs ?? {}
);

const jobTotal = computed(() => sum(jobTasks.value));
const fileTotal = computed(() => sum(fileTasks.value));

const workersOnline = computed(
  () => data.value?.workers.online ?? fallback.value?.workers_online ?? 0
);

const oldestPending = computed(
  () =>
    data.value?.oldest_pending_seconds ?? fallback.value?.pending_oldest_seconds ?? null
);

const pendingWarning = computed(
  () => (oldestPending.value ?? 0) > PENDING_WARN_SECONDS
);

const versionRows = computed(() => {
  const byVersion = data.value?.workers.by_version ?? {};
  const total = sum(byVersion) || 1;
  return Object.entries(byVersion)
    .sort((a, b) => b[1] - a[1])
    .map(([version, count]) => ({
      version,
      count,
      percent: Math.round((count / total) * 100)
    }));
});

/** 状态分布 → 进度条数据（分母用该维度的总数，没有总数时给 0） */
function distribution(
  counts: Record<string, number | undefined>,
  total: number,
  order: string[]
) {
  const keys = Object.keys(counts).filter(key => counts[key]);
  const ordered = [
    ...order.filter(item => keys.includes(item)),
    ...keys.filter(key => !order.includes(key))
  ];
  return ordered.map(status => ({
    status,
    count: counts[status] ?? 0,
    percent: total ? Math.round(((counts[status] ?? 0) / total) * 100) : 0
  }));
}

const jobDistribution = computed(() =>
  distribution(jobTasks.value, jobTotal.value, JOB_STATUS_OPTIONS)
);
const fileDistribution = computed(() =>
  distribution(fileTasks.value, fileTotal.value, TASK_STATUS_OPTIONS)
);

const degradedDescription = computed(
  () =>
    `GET /api/system/overview 请求失败（${loadError.value}）。` +
    "“作业状态分布 / 24 小时命中数 / Worker 版本分布”依赖概览接口，" +
    "本次只显示后端已有 /api/system/status 能提供的字段。"
);

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    data.value = await fetchOverview();
    fallback.value = null;
    degraded.value = false;
  } catch (error) {
    data.value = null;
    degraded.value = true;
    loadError.value = apiErrorDetail(error, "概览接口不可用");
    try {
      fallback.value = await systemStatus();
    } catch (innerError) {
      fallback.value = null;
      message(apiErrorDetail(innerError, "系统状态加载失败"), { type: "error" });
    }
  } finally {
    loadedAt.value = new Date().toLocaleTimeString();
    loading.value = false;
  }
}

async function onMaintenance() {
  try {
    await ElMessageBox.confirm(
      "执行一次维护：回收心跳超时的文件任务、把失联 Worker 记为离线、收尾停滞的作业。确认继续？",
      "执行维护",
      { type: "warning", confirmButtonText: "执行", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  maintaining.value = true;
  try {
    maintenanceResult.value = await runMaintenance();
    message(
      `维护完成：回收 ${maintenanceResult.value.recovered} 个文件任务，` +
        `降级 ${maintenanceResult.value.workers_demoted} 台 Worker，` +
        `收尾 ${maintenanceResult.value.jobs_finalized} 个作业`,
      { type: "success" }
    );
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "维护任务执行失败"), { type: "error" });
  } finally {
    maintaining.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="p-4">
    <div class="mb-3 flex items-center justify-between">
      <h2 class="text-lg font-medium">运行概览</h2>
      <div class="flex items-center gap-2">
        <span class="text-xs text-gray-500">
          {{ loading ? "加载中…" : `数据截至 ${loadedAt}` }}
        </span>
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :loading="maintaining" @click="onMaintenance">
          执行维护
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="degraded"
      class="mb-3"
      type="warning"
      :closable="false"
      show-icon
      title="概览接口不可用，已退回 /api/system/status"
      :description="degradedDescription"
    />
    <el-alert
      v-if="maintenanceResult"
      class="mb-3"
      type="success"
      :closable="true"
      show-icon
      :title="`上次维护：回收 ${maintenanceResult.recovered} 个文件任务，降级 ${maintenanceResult.workers_demoted} 台 Worker，收尾 ${maintenanceResult.jobs_finalized} 个作业`"
    />

    <el-row :gutter="12">
      <el-col :span="6">
        <el-card shadow="never">
          <div class="text-sm text-gray-500">作业总数</div>
          <div class="text-2xl font-medium">
            <template v-if="data">{{ jobTotal }}</template>
            <span v-else class="text-gray-400">未知</span>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="text-sm text-gray-500">24 小时命中行数</div>
          <div class="text-2xl font-medium">
            <template v-if="data">{{ data.matches_24h }}</template>
            <span v-else class="text-gray-400">未知</span>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="text-sm text-gray-500">存活 Worker</div>
          <div class="text-2xl font-medium">{{ workersOnline }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="text-sm text-gray-500">最老 pending 等待</div>
          <div
            class="text-2xl font-medium"
            :class="pendingWarning ? 'text-red-600' : 'text-green-700'"
          >
            {{ formatDuration(oldestPending) }}
            <el-tag v-if="pendingWarning" type="danger" size="small" effect="dark">
              积压
            </el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" class="mt-3">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>作业状态分布</template>
          <div v-if="jobDistribution.length">
            <div v-for="item in jobDistribution" :key="item.status" class="mb-2">
              <div class="flex items-center justify-between text-sm">
                <el-tag :type="statusTagType(item.status)" size="small" effect="light">
                  {{ item.status }}
                </el-tag>
                <span>{{ item.count }}（{{ item.percent }}%）</span>
              </div>
              <el-progress
                :percentage="item.percent"
                :stroke-width="10"
                :show-text="false"
              />
            </div>
            <p class="text-xs text-gray-500">共 {{ jobTotal }} 个作业</p>
          </div>
          <el-empty v-else description="概览接口未返回作业分布" :image-size="60" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>文件任务状态分布</template>
          <div v-if="fileDistribution.length">
            <div v-for="item in fileDistribution" :key="item.status" class="mb-2">
              <div class="flex items-center justify-between text-sm">
                <el-tag :type="statusTagType(item.status)" size="small" effect="light">
                  {{ item.status }}
                </el-tag>
                <span>{{ item.count }}（{{ item.percent }}%）</span>
              </div>
              <el-progress
                :percentage="item.percent"
                :stroke-width="10"
                :show-text="false"
              />
            </div>
            <p class="text-xs text-gray-500">共 {{ fileTotal }} 个文件任务</p>
          </div>
          <el-empty v-else description="暂无文件任务" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt-3">
      <template #header>Worker 版本分布</template>
      <div v-if="versionRows.length" class="flex flex-wrap gap-3">
        <el-tag v-for="item in versionRows" :key="item.version" size="large">
          {{ item.version }}：{{ item.count }} 台（{{ item.percent }}%）
        </el-tag>
      </div>
      <el-empty v-else description="概览接口未返回版本分布" :image-size="60" />
      <p class="mt-2 text-xs text-gray-500">
        存活口径与后端一致：`online|busy|draining` 都算存活，`offline` 不计。
        滚动升级请到「Worker 管理」用“按版本排空”，镜像替换由 Jenkins + Docker Compose 完成。
      </p>
    </el-card>
  </div>
</template>
