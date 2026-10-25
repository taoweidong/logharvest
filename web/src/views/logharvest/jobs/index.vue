<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import dayjs from "dayjs";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail, cancelJob, listJobs } from "@/api/logharvest";
import {
  JOB_STATUS_OPTIONS,
  TERMINAL_JOB_STATUSES,
  type Job
} from "@/api/logharvestTypes";
import { formatGB, formatDateTime, statusTagType } from "../format";
import CreateDialog from "./create-dialog.vue";

defineOptions({ name: "Jobs" });

const router = useRouter();
const loading = ref(false);
const rows = ref<Job[]>([]);
const dialogVisible = ref(false);

/** 后端列表接口是裸数组且没有 total，只能按“取满一页就可能有下一页”翻页 */
const page = reactive({ limit: 50, offset: 0 });
const statusFilter = ref<string>("");

const filteredRows = computed(() =>
  statusFilter.value
    ? rows.value.filter(item => item.status === statusFilter.value)
    : rows.value
);

const hasNext = computed(() => rows.value.length === page.limit);
const pageIndex = computed(() => Math.floor(page.offset / page.limit) + 1);

async function load() {
  loading.value = true;
  try {
    rows.value = await listJobs({ limit: page.limit, offset: page.offset });
  } catch (error) {
    rows.value = [];
    message(apiErrorDetail(error, "作业列表加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

function goNext() {
  page.offset += page.limit;
  load();
}

function goPrev() {
  page.offset = Math.max(0, page.offset - page.limit);
  load();
}

async function onCancel(row: Job) {
  try {
    await ElMessageBox.confirm(
      `确认请求取消作业 #${row.id}（${row.name}）？已进入运行中的文件任务会跑完后收尾。`,
      "取消作业",
      { type: "warning", confirmButtonText: "确认取消", cancelButtonText: "再想想" }
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

const isTerminal = (job: Job) =>
  TERMINAL_JOB_STATUSES.includes(job.status as (typeof TERMINAL_JOB_STATUSES)[number]);

const columns: TableColumnList = [
  { label: "ID", prop: "id", width: 90 },
  { label: "名称", prop: "name", minWidth: 180, showOverflowTooltip: true },
  { label: "状态", prop: "status", slot: "status", width: 110 },
  { label: "文件数", prop: "total_files", width: 100 },
  { label: "总字节", prop: "total_bytes", slot: "total_bytes", width: 130 },
  { label: "创建时间", prop: "created_at", slot: "created_at", width: 190 },
  { label: "操作", prop: "operation", slot: "operation", width: 180, fixed: "right" }
];

onMounted(load);
</script>

<template>
  <div class="p-4">
    <PureTableBar title="分析作业" :columns="columns" @refresh="load">
      <template #buttons>
        <div class="flex items-center gap-2">
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
          <el-button type="primary" @click="dialogVisible = true">
            新建作业
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
        >
          <template #status="{ row }">
            <el-tag :type="statusTagType(row.status)" :size="size" effect="light">
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
                <el-button :size="size" link type="danger" disabled>取消</el-button>
              </span>
            </el-tooltip>
            <el-button
              v-else
              :size="size"
              link
              type="danger"
              @click="onCancel(row)"
            >
              取消
            </el-button>
          </template>
        </pure-table>
        <div class="mt-3 flex items-center justify-between text-sm">
          <span class="text-gray-500">
            后端列表接口不返回总数：状态筛选只作用于当前页；第 {{ pageIndex }} 页，本页
            {{ rows.length }} 条（截至 {{ dayjs().format("HH:mm:ss") }}）
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

    <CreateDialog v-model="dialogVisible" @created="load" />
  </div>
</template>
