<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail, listJobs, listMatches } from "@/api/logharvest";
import { SEVERITY_OPTIONS, type Job, type Match } from "@/api/logharvestTypes";
import { formatDateTime, severityTagType, toApiDateTime } from "../format";

defineOptions({ name: "Matches" });

const loading = ref(false);
const rows = ref<Match[]>([]);
const jobs = ref<Job[]>([]);
const jobsFailed = ref(false);
/** 是否执行过一次查询（未查询时表格是空的，需要文案解释为什么不能直接翻） */
const searched = ref(false);

const form = reactive({
  job_id: undefined as number | undefined,
  severity: "" as string,
  created_from: null as Date | string | number | null,
  created_to: null as Date | string | number | null,
  limit: 50
});

/**
 * 后端 `GET /api/matches` 的作用域规则（`routes_matches.py:32`）：
 * `job_id` 为空时，`created_from` 与 `created_to` 必须同时给出，否则 422。
 * 前端在提交前按同一规则禁用按钮，避免无谓的 422。
 */
const hasRange = computed(() => !!form.created_from && !!form.created_to);
const scopeOk = computed(() => form.job_id !== undefined || hasRange.value);
const rangeHalf = computed(
  () => form.job_id === undefined && !!form.created_from !== !!form.created_to
);

const scopeHint = computed(() => {
  if (scopeOk.value) return "";
  if (rangeHalf.value) {
    return "时间范围必须成对：只填开始或只填结束同样不满足后端要求，会返回 422。";
  }
  return "后端要求命中结果必须限定作用域：选择某个作业，或同时给出开始与结束时间；" +
    "否则返回 422（detail: either job_id or both created_from and created_to are required）。";
});

/** 游标：`created_before` + `id_before` 必须成对（后端 `routes_matches.py:45` 校验） */
type Cursor = { created_before: string; id_before: number };

/** 本页请求携带的游标（null 表示第一屏） */
const activeCursor = ref<Cursor | null>(null);
/** 下一页要携带的游标：由本页最后一行的 `{created_at, id}` 自己构造 */
const nextCursor = ref<Cursor | null>(null);
/** 已经向后翻了几段（后端没有 offset，只能用段号做粗定位提示） */
const segment = ref(1);

const hasNext = computed(
  () => rows.value.length === form.limit && nextCursor.value !== null
);

async function load() {
  if (!scopeOk.value) return;
  loading.value = true;
  try {
    rows.value = await listMatches({
      job_id: form.job_id,
      severity: form.severity || undefined,
      created_from: toApiDateTime(form.created_from),
      created_to: toApiDateTime(form.created_to),
      created_before: activeCursor.value?.created_before,
      id_before: activeCursor.value?.id_before,
      limit: form.limit
    });
    searched.value = true;
    const last = rows.value[rows.value.length - 1];
    nextCursor.value = last
      ? { created_before: last.created_at, id_before: last.id }
      : null;
  } catch (error) {
    rows.value = [];
    nextCursor.value = null;
    message(apiErrorDetail(error, "命中结果查询失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

function onSearch() {
  activeCursor.value = null;
  nextCursor.value = null;
  segment.value = 1;
  load();
}

/** 下一页：排序固定 `created_at DESC, id DESC`，所以只能顺着游标往后走 */
function onNext() {
  if (!nextCursor.value) return;
  activeCursor.value = nextCursor.value;
  segment.value += 1;
  load();
}

/** 回到第一屏：清空游标，用当前表单条件重新查 */
function onFirst() {
  activeCursor.value = null;
  nextCursor.value = null;
  segment.value = 1;
  load();
}

async function loadJobs() {
  try {
    jobs.value = await listJobs({ limit: 200 });
    jobsFailed.value = false;
  } catch (error) {
    jobs.value = [];
    jobsFailed.value = true;
    message(apiErrorDetail(error, "作业下拉加载失败，可改用时间范围查询"), {
      type: "warning"
    });
  }
}

const columns: TableColumnList = [
  { label: "时间", prop: "created_at", width: 190 },
  { label: "作业", prop: "job_id", width: 90 },
  { label: "文件", prop: "file_name", minWidth: 180, showOverflowTooltip: true },
  {
    label: "包内路径",
    prop: "member_path",
    minWidth: 200,
    showOverflowTooltip: true
  },
  { label: "行号", prop: "line_no", width: 90 },
  { label: "规则", prop: "rule_id", minWidth: 150, showOverflowTooltip: true },
  { label: "级别", prop: "severity", width: 100 },
  { label: "数值", prop: "numeric_value", width: 120 },
  { label: "原始行", prop: "raw_line", minWidth: 320 }
];

onMounted(() => {
  loadJobs();
});
</script>

<template>
  <div class="p-4">
    <el-card shadow="never" class="mb-3">
      <el-form :inline="true" label-width="72px" @submit.prevent="onSearch">
        <el-form-item label="作业">
          <el-select
            v-model="form.job_id"
            clearable
            filterable
            placeholder="按作业过滤（可选）"
            class="w-56"
          >
            <el-option
              v-for="item in jobs"
              :key="item.id"
              :label="`#${item.id} ${item.name}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="开始">
          <el-date-picker
            v-model="form.created_from"
            type="datetime"
            placeholder="created_from"
            class="w-52"
          />
        </el-form-item>
        <el-form-item label="结束">
          <el-date-picker
            v-model="form.created_to"
            type="datetime"
            placeholder="created_to"
            class="w-52"
          />
        </el-form-item>
        <el-form-item label="级别">
          <el-select
            v-model="form.severity"
            clearable
            placeholder="severity"
            class="w-36"
          >
            <el-option
              v-for="item in SEVERITY_OPTIONS"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="每屏">
          <el-select v-model="form.limit" class="w-28">
            <el-option v-for="n in [50, 100, 200, 500]" :key="n" :label="n" :value="n" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-tooltip
            :content="scopeHint"
            placement="top"
            :disabled="scopeOk"
            :show-after="120"
          >
            <span>
              <el-button type="primary" :disabled="!scopeOk" @click="onSearch">
                查询
              </el-button>
            </span>
          </el-tooltip>
          <el-button :disabled="!searched || loading" @click="onFirst">
            回到第一屏
          </el-button>
        </el-form-item>
      </el-form>
      <el-alert
        v-if="!scopeOk"
        :title="scopeHint"
        type="warning"
        :closable="false"
        show-icon
      />
      <p class="text-xs text-gray-500">
        级别筛选是单值：后端 `severity` 只接受一个字符串（`routes_matches.py:25`），
        没有多选语义。<br />
        后端列表接口返回裸数组、不带总数，所以这里没有“共 N 条”，也没有页码跳转；
        只能顺着游标（本页最后一行的 `created_at` + `id`，按
        <code>created_at DESC, id DESC</code>）往后翻。<br />
        <span v-if="jobsFailed">作业下拉加载失败，可改用时间范围查询。</span>
      </p>
    </el-card>

    <PureTableBar title="命中结果" :columns="columns" @refresh="load">
      <template v-slot="{ size }">
        <pure-table
          :size="size"
          row-key="id"
          adaptive
          :loading="loading"
          :data="rows"
          :columns="columns"
          :border="true"
        >
          <template #created_at="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
          <template #severity="{ row }">
            <el-tag :type="severityTagType(row.severity)" :size="size" effect="light">
              {{ row.severity }}
            </el-tag>
          </template>
          <template #numeric_value="{ row }">
            {{ row.numeric_value ?? "-" }}
          </template>
          <template #raw_line="{ row }">
            <div class="flex items-start gap-2">
              <span class="match-raw">{{ row.raw_line }}</span>
              <el-tooltip
                v-if="row.raw_truncated"
                content="后端只保存截断后的原始行（raw_truncated = true）"
                placement="top"
              >
                <el-tag :size="size" type="warning" effect="plain">已截断</el-tag>
              </el-tooltip>
            </div>
          </template>
        </pure-table>
        <div class="mt-3 flex items-center justify-between text-sm">
          <span class="text-gray-500">
            <template v-if="searched">
              总数未知：第 {{ segment }} 段，本段 {{ rows.length }} 条
              <span v-if="!hasNext">（不足一屏，已到末尾）</span>
            </template>
            <template v-else>尚未查询：请先限定作业或时间范围</template>
          </span>
          <div class="flex gap-2">
            <el-button :disabled="!hasNext || loading" @click="onNext">
              下一页
            </el-button>
          </div>
        </div>
      </template>
    </PureTableBar>
  </div>
</template>

<style scoped>
.match-raw {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-all;
}
</style>
