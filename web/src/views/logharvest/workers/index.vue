<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import {
  apiErrorDetail,
  drainVersion,
  drainWorker,
  listWorkers,
  resumeWorker
} from "@/api/logharvest";
import type { WorkerInfo, WorkerStatus } from "@/api/logharvestTypes";
import {
  formatDateTime,
  fromNow,
  workerStatusTagType
} from "../format";

defineOptions({ name: "Workers" });

const loading = ref(false);
const rows = ref<WorkerInfo[]>([]);
const busyId = ref<string | null>(null);
const statusFilter = ref<string>("");

/** 后端 `Worker.status` 的全部取值（写在脚本里，模板里不做 TS 断言） */
const WORKER_STATUS_OPTIONS: WorkerStatus[] = [
  "online",
  "busy",
  "draining",
  "offline"
];

/** 后端 `GET /api/workers` 的 `limit` 上限是 200 */
const page = reactive({ limit: 100, offset: 0 });
const hasNext = computed(() => rows.value.length === page.limit);

/** 与后端 `system_status` 同一定义：未离线即视为存活（含 draining） */
const alive = computed(
  () =>
    rows.value.filter(item =>
      (["online", "busy", "draining"] as WorkerStatus[]).includes(item.status)
    ).length
);

const versions = computed(() =>
  Array.from(new Set(rows.value.map(item => item.version))).sort()
);

const filteredRows = computed(() =>
  statusFilter.value
    ? rows.value.filter(item => item.status === statusFilter.value)
    : rows.value
);

async function load() {
  loading.value = true;
  try {
    rows.value = await listWorkers({ limit: page.limit, offset: page.offset });
  } catch (error) {
    rows.value = [];
    message(apiErrorDetail(error, "Worker 列表加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

/**
 * 单台排空：后端把 `status` 改成 `draining`（离线行只记 `drain_requested`，
 * 状态保持 `offline`，见 `routes_workers.py:17`）。
 */
async function onDrain(row: WorkerInfo) {
  try {
    await ElMessageBox.confirm(
      `确认排空 Worker ${row.id}？它跑完当前文件任务后不再领取新任务。`,
      "排空 Worker",
      { type: "warning", confirmButtonText: "排空", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  busyId.value = row.id;
  try {
    const result = await drainWorker(row.id);
    message(
      `${result.id}：drain_requested=${result.drain_requested}，状态 ${result.status}`,
      { type: "success" }
    );
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "排空失败"), { type: "error" });
  } finally {
    busyId.value = null;
  }
}

/** 恢复领取：后端对 `offline` Worker 返回 409，所以离线的行直接禁用按钮 */
async function onResume(row: WorkerInfo) {
  busyId.value = row.id;
  try {
    const result = await resumeWorker(row.id);
    message(`${result.id} 已恢复领取（状态 ${result.status}）`, {
      type: "success"
    });
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "恢复失败"), { type: "error" });
  } finally {
    busyId.value = null;
  }
}

const versionInput = ref("");
const drainingVersion = ref(false);

/** 按版本排空（滚动升级用）：`drained` 是真正转入 draining 的存活机器，`flagged` 是只打了标记的离线机器 */
async function onDrainVersion() {
  const version = versionInput.value.trim();
  if (!version) {
    message("请先填写要排空的 Worker 版本号", { type: "warning" });
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认排空版本 ${version} 的全部 Worker？离线机器也会被记下标记，重启后生效。`,
      "按版本排空",
      { type: "warning", confirmButtonText: "排空", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  drainingVersion.value = true;
  try {
    const result = await drainVersion(version);
    message(
      `版本 ${version}：已排空 ${result.drained} 台，标记 ${result.flagged} 台离线（重启后生效）`,
      { type: "success" }
    );
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "按版本排空失败"), { type: "error" });
  } finally {
    drainingVersion.value = false;
  }
}

const columns: TableColumnList = [
  { label: "Worker", prop: "id", minWidth: 200, showOverflowTooltip: true },
  { label: "主机名", prop: "hostname", minWidth: 160, showOverflowTooltip: true },
  { label: "版本", prop: "version", width: 130 },
  { label: "状态", prop: "status", width: 110 },
  { label: "当前文件任务", prop: "current_task_id", width: 130 },
  { label: "并发上限", prop: "max_concurrent_copies", width: 110 },
  { label: "心跳", prop: "last_heartbeat", width: 190 },
  { label: "排空标记", prop: "drain_requested", width: 110 },
  { label: "操作", prop: "operation", width: 170, fixed: "right" }
];

onMounted(load);
</script>

<template>
  <div class="p-4">
    <el-alert
      class="mb-3"
      type="info"
      :closable="false"
      show-icon
      title="这里不执行任何部署"
      description="本界面只做「排空 / 恢复领取」的运维标记。Worker 镜像的替换与重启由
        Jenkins 流水线 + Docker Compose 完成；「按版本排空」用于滚动升级时先让旧版本的
        Worker 跑完手头的文件任务，离线机器上的标记会在其重启后生效。"
    />

    <PureTableBar title="Worker" :columns="columns" @refresh="load">
      <template #buttons>
        <div class="flex items-center gap-2">
          <el-select v-model="statusFilter" clearable placeholder="状态筛选" class="w-36">
            <el-option
              v-for="item in WORKER_STATUS_OPTIONS"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
          <el-select
            v-model="versionInput"
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="版本号"
            class="w-40"
          >
            <el-option v-for="v in versions" :key="v" :label="v" :value="v" />
          </el-select>
          <el-button
            type="warning"
            :loading="drainingVersion"
            :disabled="!versionInput.trim()"
            @click="onDrainVersion"
          >
            按版本排空
          </el-button>
        </div>
      </template>
      <template v-slot="{ size }">
        <pure-table
          :size="size"
          row-key="id"
          adaptive
          :loading="loading"
          :data="filteredRows"
          :columns="columns"
          :border="true"
        >
          <template #status="{ row }">
            <el-tag
              :type="workerStatusTagType(row.status)"
              :size="size"
              effect="light"
            >
              {{ row.status }}
            </el-tag>
          </template>
          <template #current_task_id="{ row }">
            {{ row.current_task_id ?? "-" }}
          </template>
          <template #last_heartbeat="{ row }">
            <el-tooltip :content="formatDateTime(row.last_heartbeat)" placement="top">
              <span>{{ fromNow(row.last_heartbeat) }}</span>
            </el-tooltip>
          </template>
          <template #drain_requested="{ row }">
            <el-tag
              :size="size"
              :type="row.drain_requested ? 'warning' : 'info'"
              effect="plain"
            >
              {{ row.drain_requested ? "已标记" : "无" }}
            </el-tag>
          </template>
          <template #operation="{ row }">
            <el-button
              :size="size"
              link
              type="warning"
              :loading="busyId === row.id"
              :disabled="row.drain_requested"
              @click="onDrain(row)"
            >
              排空
            </el-button>
            <el-tooltip
              v-if="row.status === 'offline'"
              content="Worker 已离线，后端对离线机器恢复领取返回 409"
              placement="top"
            >
              <span>
                <el-button :size="size" link type="primary" disabled>恢复</el-button>
              </span>
            </el-tooltip>
            <el-button
              v-else
              :size="size"
              link
              type="primary"
              :loading="busyId === row.id"
              @click="onResume(row)"
            >
              恢复
            </el-button>
          </template>
        </pure-table>
        <div class="mt-3 flex items-center justify-between text-sm">
          <span class="text-gray-500">
            后端列表接口不返回总数：状态筛选只作用于本页；第
            {{ Math.floor(page.offset / page.limit) + 1 }} 页，本页
            {{ rows.length }} 条，其中存活（online/busy/draining）{{ alive }} 台
          </span>
          <div class="flex gap-2">
            <el-button
              :disabled="page.offset === 0 || loading"
              @click="(page.offset -= page.limit), load()"
            >
              上一页
            </el-button>
            <el-button
              :disabled="!hasNext || loading"
              @click="(page.offset += page.limit), load()"
            >
              下一页
            </el-button>
          </div>
        </div>
      </template>
    </PureTableBar>
  </div>
</template>
