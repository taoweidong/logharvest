<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail, listJobs, listMatches } from "@/api/logharvest";
import { SEVERITY_OPTIONS, type Job, type Match } from "@/api/logharvestTypes";
import { formatDateTime, severityTagType, toApiDateTime } from "../format";
import { useAppStoreHook } from "@/store/modules/app";
import { TABLE_HEADER_CELL_STYLE } from "@/config/table";

defineOptions({ name: "Matches" });

const route = useRoute();
const router = useRouter();
const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const loading = ref(false);
const rows = ref<Match[]>([]);
const jobs = ref<Job[]>([]);
const jobsFailed = ref(false);
/** 是否执行过一次查询（未查询时表格是空的，需要文案解释为什么不能直接翻） */
const searched = ref(false);

/**
 * 作用域显式二选一。后端 `GET /api/matches`（`routes_matches.py`）要求
 * `job_id`，或者 `created_from` + `created_to` 同时给出，否则 422；
 * 这里不让两个条件混着填，避免用户以为“都填了一点也行”。
 */
const scopeMode = ref<"job" | "range">("job");

const form = reactive({
  job_id: undefined as number | undefined,
  severity: "" as string,
  created_from: null as Date | string | number | null,
  created_to: null as Date | string | number | null,
  limit: 50,
});

const hasRange = computed(() => !!form.created_from && !!form.created_to);
const scopeOk = computed(() =>
  scopeMode.value === "job" ? form.job_id !== undefined : hasRange.value,
);
/** 时间范围只填了一半：明确告诉用户后端会 422，而不是让他试出来 */
const rangeHalf = computed(
  () =>
    scopeMode.value === "range" && !!form.created_from !== !!form.created_to,
);

const scopeHint = computed(() => {
  if (scopeOk.value) return "";
  if (scopeMode.value === "job") {
    return "按作业查询需要选中一个作业。后端契约：没有 job_id 时必须同时给出 created_from 与 created_to，否则 422（either job_id or both created_from and created_to are required）。";
  }
  if (rangeHalf.value) {
    return "时间范围必须成对：只填开始或只填结束同样不满足后端要求，会返回 422。";
  }
  return "按时间范围查询需要同时给出开始与结束。后端契约：没有 job_id 时必须成对给出 created_from 与 created_to，否则 422。";
});

/** 切换作用域时清掉另一侧的残留值，避免带着看不见的条件去查询 */
function onScopeModeChange() {
  if (scopeMode.value === "job") {
    form.created_from = null;
    form.created_to = null;
  } else {
    form.job_id = undefined;
  }
  rows.value = [];
  searched.value = false;
  activeCursor.value = null;
  nextCursor.value = null;
  segment.value = 1;
}

/** 游标：`created_before` + `id_before` 必须成对（后端 `routes_matches.py:45` 校验） */
type Cursor = { created_before: string; id_before: number };

/** 本页请求携带的游标（null 表示第一屏） */
const activeCursor = ref<Cursor | null>(null);
/** 下一页要携带的游标：由本页最后一行的 `{created_at, id}` 自己构造 */
const nextCursor = ref<Cursor | null>(null);
/** 已经向后翻了几段（后端没有 offset，只能用段号做粗定位提示） */
const segment = ref(1);

const hasNext = computed(
  () => rows.value.length === form.limit && nextCursor.value !== null,
);

async function load() {
  if (!scopeOk.value) return;
  loading.value = true;
  try {
    rows.value = await listMatches({
      job_id: scopeMode.value === "job" ? form.job_id : undefined,
      severity: form.severity || undefined,
      created_from: toApiDateTime(form.created_from),
      created_to: toApiDateTime(form.created_to),
      created_before: activeCursor.value?.created_before,
      id_before: activeCursor.value?.id_before,
      limit: form.limit,
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
      type: "warning",
    });
  }
}

/** 「所属作业」链接 */
function goJob(jobId: number) {
  router.push(`/logharvest/jobs/${jobId}`);
}

/**
 * 「所属文件」链接：文件任务只能在作业详情里看，
 * 带上 `file_task` 让详情页直接打开该任务的尝试历史。
 */
function goFileTask(row: Match) {
  router.push({
    path: `/logharvest/jobs/${row.job_id}`,
    query: { file_task: String(row.file_task_id) },
  });
}

const columns = computed<TableColumnList>(() => [
  // @pureadmin/table 3.3.0 只解析 column.slot，不声明则 <template #prop> 静默不渲染。
  {
    label: "时间",
    prop: "created_at",
    slot: "created_at",
    width: isMobile.value ? 150 : 190,
  },
  { label: "所属作业", prop: "job_id", slot: "job_id", width: 110 },
  {
    label: "所属文件",
    prop: "file_name",
    slot: "file_name",
    minWidth: isMobile.value ? 140 : 180,
    showOverflowTooltip: true,
  },
  {
    label: "包内路径",
    prop: "member_path",
    minWidth: 200,
    showOverflowTooltip: true,
  },
  { label: "行号", prop: "line_no", width: 90 },
  {
    label: "规则",
    prop: "rule_id",
    minWidth: 150,
    showOverflowTooltip: true,
  },
  { label: "级别", prop: "severity", slot: "severity", width: 100 },
  { label: "数值", prop: "numeric_value", slot: "numeric_value", width: 120 },
  { label: "原始行", prop: "raw_line", slot: "raw_line", minWidth: 320 },
]);

onMounted(async () => {
  // 作业详情「查看命中结果」带 job_id 跳过来：直接按该作业生效
  const jobId = Number(
    typeof route.query.job_id === "string" ? route.query.job_id : "",
  );
  if (Number.isInteger(jobId) && jobId > 0) {
    scopeMode.value = "job";
    form.job_id = jobId;
  }
  await loadJobs();
  if (form.job_id !== undefined) onSearch();
});
</script>

<template>
  <div class="page">
    <section class="page-panel">
      <el-form
        :inline="true"
        label-width="72px"
        class="search-form"
        @submit.prevent="onSearch"
      >
        <el-form-item label="作用域">
          <el-radio-group v-model="scopeMode" @change="onScopeModeChange">
            <el-radio-button value="job">按作业</el-radio-button>
            <el-radio-button value="range">按时间范围</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="scopeMode === 'job'" label="作业">
          <el-select
            v-model="form.job_id"
            clearable
            filterable
            placeholder="选择要查看的作业"
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
        <template v-else>
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
        </template>
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
            <el-option
              v-for="n in [50, 100, 200, 500]"
              :key="n"
              :label="n"
              :value="n"
            />
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
        class="mt-3"
      />

      <p class="page-hint mt-3">
        作用域必须二选一：<code>/api/matches</code> 要么按
        <code>job_id</code> 查，要么成对给出 <code>created_from</code> +
        <code>created_to</code>，只填一半返回 422，所以查询按钮会先禁用。<br />
        级别筛选是单值：<code>severity</code>
        只接受一个字符串，没有多选语义。<br />
        列表接口返回裸数组、不带总数，所以这里没有“共 N 条”，也没有页码跳转；
        只能顺着游标（本页最后一行的 <code>created_at</code> +
        <code>id</code>，按
        <code>created_at DESC, id DESC</code>）往后翻。<br />
        <span v-if="jobsFailed">作业下拉加载失败，可改用时间范围查询。</span>
      </p>
    </section>

    <!-- 窄屏（390 视口）表格横向放不下，改成卡片：一条命中一张卡，链接与字段都保留 -->
    <div v-if="isMobile" class="page-panel">
      <div class="page-body space-y-3">
        <el-card
          v-for="row in rows"
          :key="row.id"
          shadow="never"
          body-class="!p-3"
        >
          <div class="flex items-center justify-between gap-2">
            <el-tag
              :type="severityTagType(row.severity)"
              size="small"
              effect="light"
            >
              {{ row.severity }}
            </el-tag>
            <span class="page-hint">{{ formatDateTime(row.created_at) }}</span>
          </div>
          <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
            <el-button link type="primary" @click="goJob(row.job_id)">
              所属作业 #{{ row.job_id }}
            </el-button>
            <el-button link type="primary" @click="goFileTask(row)">
              所属文件 {{ row.file_name }}
            </el-button>
            <span>行号 {{ row.line_no }}</span>
            <span>规则 {{ row.rule_id }}</span>
            <span>数值 {{ row.numeric_value ?? "-" }}</span>
          </div>
          <div class="page-hint mt-1">包内路径 {{ row.member_path }}</div>
          <pre class="match-raw mt-2">{{ row.raw_line }}</pre>
          <el-tag
            v-if="row.raw_truncated"
            size="small"
            type="warning"
            effect="plain"
          >
            原始行已截断
          </el-tag>
        </el-card>
        <el-empty
          v-if="!loading && rows.length === 0"
          :description="searched ? '本段没有命中行' : '尚未查询'"
        />
      </div>
      <div class="page-foot">
        <span>
          <template v-if="searched">
            总数未知：第 {{ segment }} 段，本段 {{ rows.length }} 条
            <span v-if="!hasNext">（不足一屏，已到末尾）</span>
          </template>
          <template v-else>尚未查询：请先限定作业或时间范围</template>
        </span>
        <div class="page-foot__actions">
          <el-button :disabled="!hasNext || loading" @click="onNext">
            下一页
          </el-button>
        </div>
      </div>
    </div>

    <PureTableBar v-else title="命中结果" :columns="columns" @refresh="load">
      <template v-slot="{ size }">
        <pure-table
          :size="size"
          row-key="id"
          height="100%"
          :loading="loading"
          :data="rows"
          :columns="columns"
          :border="true"
          :header-cell-style="TABLE_HEADER_CELL_STYLE"
        >
          <template #created_at="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
          <template #job_id="{ row }">
            <el-button
              :size="size"
              link
              type="primary"
              @click="goJob(row.job_id)"
            >
              #{{ row.job_id }}
            </el-button>
          </template>
          <template #file_name="{ row }">
            <el-button
              :size="size"
              link
              type="primary"
              @click="goFileTask(row)"
            >
              {{ row.file_name }}
            </el-button>
          </template>
          <template #severity="{ row }">
            <el-tag
              :type="severityTagType(row.severity)"
              :size="size"
              effect="light"
            >
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
                <el-tag :size="size" type="warning" effect="plain"
                  >已截断</el-tag
                >
              </el-tooltip>
            </div>
          </template>
        </pure-table>
      </template>
      <template #footer>
        <span>
          <template v-if="searched">
            总数未知：第 {{ segment }} 段，本段 {{ rows.length }} 条
            <span v-if="!hasNext">（不足一屏，已到末尾）</span>
          </template>
          <template v-else>尚未查询：请先限定作业或时间范围</template>
        </span>
        <div class="page-foot__actions">
          <el-button :disabled="!hasNext || loading" @click="onNext">
            下一页
          </el-button>
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
  white-space: pre-wrap;
}
</style>
