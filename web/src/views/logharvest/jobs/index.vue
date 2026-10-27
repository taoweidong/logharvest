<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import type { LocationQueryValue } from "vue-router";
import dayjs from "dayjs";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail, cancelJob, listJobs } from "@/api/logharvest";
import {
  JOB_STATUS_OPTIONS,
  TERMINAL_JOB_STATUSES,
  type Job,
  type JobStatus,
} from "@/api/logharvestTypes";
import { formatGB, formatDateTime, statusTagType } from "../format";
import { hasPerms } from "@/utils/auth";
import { useAppStoreHook } from "@/store/modules/app";
import CreateDialog from "./create-dialog.vue";

defineOptions({ name: "Jobs" });

const route = useRoute();
const router = useRouter();
const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const loading = ref(false);
const rows = ref<Job[]>([]);
const dialogVisible = ref(false);
/** 数据源页「用它建任务」带过来的 `data_source_id`，向导第①步预填 */
const prefillDataSourceId = ref<number | null>(null);

/** 后端列表接口是裸数组且没有 total，只能按“取满一页就可能有下一页”翻页 */
const page = reactive({ limit: 50, offset: 0 });

/**
 * 状态筛选下推给后端（`?status=`）：以前在前端只过滤当前页 20/50 条，
 * 用户看到的是“筛了但没筛完”的假结果，这是可感知的错误行为。
 */
const statusFilter = ref<JobStatus | "">("");

/** 关键字与 UTC 日提示：后端 `/jobs` 没有这两个参数，只能限定在当前页内，界面如实标注 */
const keyword = ref("");
const dayHint = ref("");

function queryString(
  value: LocationQueryValue | LocationQueryValue[] | undefined,
) {
  return typeof value === "string" ? value : "";
}

/** 后端时间是不带时区的 UTC 墙上时间，前 10 位就是它所属的 UTC 日 */
const utcDay = (iso: string) => iso.slice(0, 10);

const displayRows = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return rows.value.filter((row) => {
    if (dayHint.value && utcDay(row.created_at) !== dayHint.value) return false;
    if (!kw) return true;
    return `${row.id} ${row.name} ${row.source_path}`
      .toLowerCase()
      .includes(kw);
  });
});

const hasNext = computed(() => rows.value.length === page.limit);
const pageIndex = computed(() => Math.floor(page.offset / page.limit) + 1);

async function load() {
  loading.value = true;
  try {
    rows.value = await listJobs({
      limit: page.limit,
      offset: page.offset,
      ...(statusFilter.value ? { status: statusFilter.value } : {}),
    });
  } catch (error) {
    rows.value = [];
    message(apiErrorDetail(error, "作业列表加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

/** 筛选条件变了就回到第一页，否则 offset 还停在上一轮的窗口里 */
function onSearch() {
  page.offset = 0;
  load();
}

function goNext() {
  page.offset += page.limit;
  load();
}

function goPrev() {
  page.offset = Math.max(0, page.offset - page.limit);
  load();
}

function clearDayHint() {
  dayHint.value = "";
  // URL 里的 date 是统计页跳转带来的，清除后要同步抹掉，否则刷新又回来
  router.replace({ query: { ...route.query, date: undefined } });
}

async function onCancel(row: Job) {
  try {
    await ElMessageBox.confirm(
      `确认请求取消作业 #${row.id}（${row.name}）？已进入运行中的文件任务会跑完后收尾。`,
      "取消作业",
      {
        type: "warning",
        confirmButtonText: "确认取消",
        cancelButtonText: "再想想",
      },
    );
  } catch {
    return;
  }
  try {
    const job = await cancelJob(row.id);
    message(`作业 #${job.id} 当前状态：${job.status}`, { type: "success" });
    load();
  } catch (error) {
    message(apiErrorDetail(error, "取消失败"), { type: "error" });
  }
}

/** 向导提交成功：先刷新列表，再跳详情页由详情页起 SSE 实时进度 */
function onCreated(job: Job) {
  dialogVisible.value = false;
  prefillDataSourceId.value = null;
  router.push(`/logharvest/jobs/${job.id}`);
}

const isTerminal = (job: Job) =>
  TERMINAL_JOB_STATUSES.includes(
    job.status as (typeof TERMINAL_JOB_STATUSES)[number],
  );

/**
 * `@pureadmin/table` 3.3.0 只按列定义里的 `slot` 取内容插槽，不写 `slot` 的列
 * 会退回裸文本；窄屏（390 视口）隐藏非关键列，操作列取消固定，否则表格横向溢出。
 */
const columns = computed<TableColumnList>(() => [
  ...(isMobile.value
    ? []
    : [{ label: "ID", prop: "id", width: 90 } as TableColumnList[number]]),
  {
    label: "名称",
    prop: "name",
    minWidth: isMobile.value ? 140 : 180,
    showOverflowTooltip: true,
  },
  { label: "状态", prop: "status", slot: "status", width: 110 },
  ...(isMobile.value
    ? []
    : ([
        { label: "文件数", prop: "total_files", width: 100 },
        {
          label: "总字节",
          prop: "total_bytes",
          slot: "total_bytes",
          width: 130,
        },
      ] as TableColumnList)),
  {
    label: "创建时间",
    prop: "created_at",
    slot: "created_at",
    width: isMobile.value ? 160 : 190,
  },
  {
    label: "操作",
    prop: "operation",
    slot: "operation",
    width: isMobile.value ? 120 : 180,
    fixed: isMobile.value ? undefined : "right",
  },
]);

/**
 * 消费其它页面跳过来时带的 query：
 * - `status`：统计页的状态计数，非法值忽略（后端会对非法值返回 422）
 * - `date`：统计页条形点击带来的 UTC 日，作为可清除的提示
 * - `data_source_id`：数据源页「用它建任务」，直接打开向导并预填第①步
 */
function applyQuery() {
  const query = route.query;
  const status = queryString(query.status);
  if ((JOB_STATUS_OPTIONS as string[]).includes(status)) {
    statusFilter.value = status as JobStatus;
  }
  const date = queryString(query.date);
  if (/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    dayHint.value = date;
  }
  const dataSourceId = Number(queryString(query.data_source_id));
  if (
    Number.isInteger(dataSourceId) &&
    dataSourceId > 0 &&
    hasPerms("jobs:write")
  ) {
    prefillDataSourceId.value = dataSourceId;
    dialogVisible.value = true;
  }
}

onMounted(async () => {
  applyQuery();
  await load();
});
</script>

<template>
  <div class="p-4">
    <PureTableBar title="分析作业" :columns="columns" @refresh="load">
      <template #buttons>
        <div class="flex flex-wrap items-center justify-end gap-2">
          <div class="w-[180px] sm:w-[220px]">
            <el-input
              v-model="keyword"
              clearable
              placeholder="关键字（本页）"
              @keyup.enter="onSearch"
              @clear="onSearch"
            />
          </div>
          <!-- 宽度挂在包裹层上：el-select 根节点自带 width:100%，直接给组件加 w-32
               会被盖掉，在 flex-wrap 容器里就会各占一行。 -->
          <div class="w-32 sm:w-36">
            <el-select
              v-model="statusFilter"
              clearable
              placeholder="状态筛选"
              class="w-full"
              @change="onSearch"
            >
              <el-option
                v-for="item in JOB_STATUS_OPTIONS"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </div>
          <el-tag
            v-if="dayHint"
            closable
            type="info"
            effect="plain"
            @close="clearDayHint"
          >
            UTC 日：{{ dayHint }}
          </el-tag>
          <el-button
            v-perms="'jobs:write'"
            type="primary"
            @click="dialogVisible = true"
          >
            新建任务
          </el-button>
        </div>
      </template>
      <template v-slot="{ size }">
        <pure-table
          :size="size"
          row-key="id"
          adaptive
          :loading="loading"
          :data="displayRows"
          :columns="columns"
        >
          <template #status="{ row }">
            <el-tag
              :type="statusTagType(row.status)"
              :size="size"
              effect="light"
            >
              {{ row.status }}
            </el-tag>
          </template>
          <template #total_bytes="{ row }">
            {{ formatGB(row.total_bytes) }}
          </template>
          <template #created_at="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
          <template #operation="{ row }">
            <el-button
              :size="size"
              link
              type="primary"
              @click="router.push(`/logharvest/jobs/${row.id}`)"
            >
              详情
            </el-button>
            <el-tooltip
              v-if="isTerminal(row)"
              content="作业已进入终态，后端不会改写"
              placement="top"
            >
              <span>
                <el-button
                  v-perms="'jobs:operate'"
                  :size="size"
                  link
                  type="danger"
                  disabled
                >
                  取消
                </el-button>
              </span>
            </el-tooltip>
            <el-button
              v-else
              v-perms="'jobs:operate'"
              :size="size"
              link
              type="danger"
              @click="onCancel(row)"
            >
              取消
            </el-button>
          </template>
        </pure-table>
        <div
          class="mt-3 flex flex-wrap items-center justify-between gap-2 text-sm"
        >
          <span class="text-gray-500">
            状态已下推到后端（<code>?status=</code>，非法值 422）；关键字与「UTC
            日」提示 只在当前页内过滤（后端
            <code>/jobs</code> 没有这两个参数）。列表接口不返回总数： 第
            {{ pageIndex }} 页，本页 {{ rows.length }} 条，其中符合提示条件
            {{ displayRows.length }} 条（截至 {{ dayjs().format("HH:mm:ss") }}）
          </span>
          <div class="flex gap-2">
            <el-button :disabled="page.offset === 0 || loading" @click="goPrev">
              上一页
            </el-button>
            <el-button :disabled="!hasNext || loading" @click="goNext">
              下一页
            </el-button>
          </div>
        </div>
      </template>
    </PureTableBar>

    <CreateDialog
      v-model="dialogVisible"
      :prefill-data-source-id="prefillDataSourceId"
      @created="onCreated"
    />
  </div>
</template>
