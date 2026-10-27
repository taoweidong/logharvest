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
  UNKNOWN_TEXT,
  formatCpuPercent,
  formatDateTime,
  formatDiskFreeGB,
  formatMemoryMB,
  fromNow,
  workerStatusTagType
} from "../format";
import { useAppStoreHook } from "@/store/modules/app";

defineOptions({ name: "Workers" });

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

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
const pageIndex = computed(() => Math.floor(page.offset / page.limit) + 1);

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

/**
 * 按 `worker_version`（接口字段 `version`）分组：滚动升级时一眼看出哪些版本还在跑、
 * 每台的状态如何，比平铺一列版本号好读。版本排序按出现顺序倒序即可（无 semver 语义保证）。
 */
const groups = computed(() => {
  const byVersion = new Map<string, WorkerInfo[]>();
  filteredRows.value.forEach(item => {
    const list = byVersion.get(item.version);
    if (list) list.push(item);
    else byVersion.set(item.version, [item]);
  });
  return Array.from(byVersion.entries())
    .sort((a, b) => b[0].localeCompare(a[0]))
    .map(([version, items]) => ({
      version,
      items,
      alive: items.filter(
        item => (["online", "busy", "draining"] as WorkerStatus[]).includes(item.status)
      ).length
    }));
});

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

/**
 * `@pureadmin/table` 3.3.0 只认列定义里的 `slot` 字段（不写 `slot` 时，与 `prop` 同名的
 * 插槽模板会被静默忽略，单元格退回裸文本），所以每一列都要显式写 `slot`。
 * 版本已经提到分组头上，组内表不再重复显示。
 */
const columns = computed<TableColumnList>(() => [
  {
    label: "Worker",
    prop: "id",
    minWidth: isMobile.value ? 140 : 200,
    showOverflowTooltip: true
  },
  ...(isMobile.value
    ? []
    : ([
        {
          label: "主机名",
          prop: "hostname",
          minWidth: 160,
          showOverflowTooltip: true
        }
      ] as TableColumnList)),
  { label: "状态", prop: "status", slot: "status", width: 110 },
  {
    label: "当前文件任务",
    prop: "current_task_id",
    slot: "current_task_id",
    width: 130
  },
  ...(isMobile.value
    ? []
    : ([
        { label: "并发上限", prop: "max_concurrent_copies", width: 110 },
        { label: "CPU", prop: "cpu_percent", slot: "cpu_percent", width: 100 },
        { label: "内存", prop: "memory_bytes", slot: "memory_bytes", width: 120 },
        {
          label: "临时盘空闲",
          prop: "temp_disk_free_bytes",
          slot: "temp_disk_free_bytes",
          width: 140
        }
      ] as TableColumnList)),
  { label: "心跳", prop: "last_heartbeat", slot: "last_heartbeat", width: 190 },
  {
    label: "排空标记",
    prop: "drain_requested",
    slot: "drain_requested",
    width: 110
  },
  ...(isMobile.value
    ? []
    : ([
        {
          label: "最近错误",
          prop: "last_error",
          slot: "last_error",
          minWidth: 180,
          showOverflowTooltip: true
        }
      ] as TableColumnList)),
  {
    label: "操作",
    prop: "operation",
    slot: "operation",
    width: isMobile.value ? 120 : 170,
    fixed: isMobile.value ? undefined : "right"
  }
]);

/**
 * 资源三列与 `last_error` 都是可空列（见 `WorkerRead`）：M2c Task 4 之前心跳语句
 * 只更新 `status`/`current_task_id`/`last_heartbeat`，所以老行与未重启的 Worker 这里是
 * `NULL`。表头不做任何“默认 0”的兜底，`null` 一律走 `format*` 里的 `—`。
 */
const metricReported = computed(
  () =>
    filteredRows.value.filter(
      item =>
        typeof item.cpu_percent === "number" ||
        typeof item.memory_bytes === "number" ||
        typeof item.temp_disk_free_bytes === "number"
    ).length
);

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

    <el-alert
      class="mb-3"
      type="info"
      :closable="false"
      show-icon
      title="CPU / 内存 / 临时盘空闲 / 最近错误：`—` 是「没有上报过」，不是 0"
      description="这四列在 workers 表里一直存在，但直到 M2c Task 4 才有写入方——此前的心跳语句只更新
        status / current_task_id / last_heartbeat。因此升级前留下的行、以及升级后还没重启的 Worker
        都会显示破折号（未知）；把它读成「CPU 空闲」或「临时盘已满」就是本界面刻意避免的误判。
        数值来自 Worker 自身采样（进程 CPU/内存 RSS、临时目录所在盘的剩余空间），不是 NAS 侧指标。"
    />

    <PureTableBar title="Worker（按版本分组）" :columns="columns" @refresh="load">
      <template #buttons>
        <div class="flex flex-wrap items-center justify-end gap-2">
          <el-select
            v-model="statusFilter"
            clearable
            placeholder="状态筛选"
            class="w-32 sm:w-36"
          >
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
            class="w-36 sm:w-40"
          >
            <el-option v-for="v in versions" :key="v" :label="v" :value="v" />
          </el-select>
          <el-button
            v-perms="'workers:operate'"
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
        <el-empty
          v-if="!loading && groups.length === 0"
          description="没有可见的 Worker：请先启动 Worker 进程（python -m src.worker.main）"
          :image-size="80"
        />
        <el-card
          v-for="group in groups"
          :key="group.version"
          class="mb-3"
          shadow="never"
        >
          <template #header>
            <div class="flex flex-wrap items-center justify-between gap-2">
              <span class="text-sm font-medium">
                版本 {{ group.version }}
                <el-tag :size="size" effect="plain" type="info">
                  {{ group.items.length }} 台
                </el-tag>
              </span>
              <span class="text-xs text-gray-500">
                存活（online/busy/draining）{{ group.alive }} 台 · 离线
                {{ group.items.length - group.alive }} 台
                <el-button
                  v-perms="'workers:operate'"
                  link
                  type="warning"
                  :size="size"
                  @click="(versionInput = group.version), onDrainVersion()"
                >
                  排空该版本
                </el-button>
              </span>
            </div>
          </template>
          <pure-table
            :size="size"
            row-key="id"
            adaptive
            :loading="loading"
            :data="group.items"
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
            <template #cpu_percent="{ row }">
              {{ formatCpuPercent(row.cpu_percent) }}
            </template>
            <template #memory_bytes="{ row }">
              {{ formatMemoryMB(row.memory_bytes) }}
            </template>
            <template #temp_disk_free_bytes="{ row }">
              {{ formatDiskFreeGB(row.temp_disk_free_bytes) }}
            </template>
            <template #last_error="{ row }">
              <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top">
                <span class="text-red-500">{{ row.last_error }}</span>
              </el-tooltip>
              <span v-else>{{ UNKNOWN_TEXT }}</span>
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
                v-perms="'workers:operate'"
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
                  <el-button
                    v-perms="'workers:operate'"
                    :size="size"
                    link
                    type="primary"
                    disabled
                  >
                    恢复
                  </el-button>
                </span>
              </el-tooltip>
              <el-button
                v-else
                v-perms="'workers:operate'"
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
        </el-card>
        <div class="mt-3 flex flex-wrap items-center justify-between gap-2 text-sm">
          <span class="text-gray-500">
            后端列表接口不返回总数：状态筛选只作用于本页；第
            {{ pageIndex }} 页，本页 {{ rows.length }} 条，共 {{ groups.length }}
            个版本，存活（online/busy/draining）{{ alive }} 台；至少上报过一项资源的
            {{ metricReported }} 台（其余显示「{{ UNKNOWN_TEXT }}」= 该 Worker
            尚未上报，需升级到 M2c Task 4 的 Worker 并重启后才有数据）
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
