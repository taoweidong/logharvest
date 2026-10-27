<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { message } from "@/utils/message";
import { apiErrorDetail, apiErrorStatus } from "@/api/logharvest";
import { getJobStats } from "@/api/analytics";
import type { JobStatsDailyRow, JobStatsResult } from "@/api/analyticsTypes";
import { listDataSources } from "@/api/logharvestAdmin";
import type { DataSource } from "@/api/logharvestTypes";
import { formatGB, statusTagType } from "../format";
import { useAppStoreHook } from "@/store/modules/app";

defineOptions({ name: "TaskStats" });

const router = useRouter();
const loading = ref(false);
const data = ref<JobStatsResult | null>(null);
/** 接口 403（无 jobs:read）时整页降级，不再渲染残缺的卡片与表格 */
const forbidden = ref(false);

/** days 后端限定 1–90，页面只提供设计约定的三档快捷窗口 */
const DAYS_OPTIONS = [7, 14, 30];
const days = ref(14);
const dataSourceId = ref<number | undefined>(undefined);
const dataSources = ref<DataSource[]>([]);

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const totals = computed(() => data.value?.totals ?? null);
const daily = computed(() => data.value?.daily ?? []);
const topRules = computed(() => data.value?.top_rules ?? []);

/** 全 0 判定用作业总数而非天数：窗口里一天没跑活也要给明确的空态文案 */
const isEmpty = computed(() => !!data.value && data.value.totals.jobs === 0);

/** 失败率 = files_failed / (file_succeeded + files_failed)；分母为 0 显示未知，不冒充 0% */
const failureRate = computed(() => {
  const t = totals.value;
  if (!t) return null;
  const done = t.file_succeeded + t.file_failed;
  if (done <= 0) return null;
  return (t.file_failed / done) * 100;
});

/** 条形按命中行数归一；分母至少为 1，避免除零且让全 0 的天也有可见的基线 */
const maxMatched = computed(() =>
  daily.value.reduce((max, row) => Math.max(max, row.matched_rows), 1),
);

function barPercent(row: JobStatsDailyRow): number {
  const percent = Math.round((row.matched_rows / maxMatched.value) * 100);
  // 有命中但占比极小时至少给 2% 宽度，否则条形视觉上等于不存在
  return row.matched_rows > 0 ? Math.max(percent, 2) : percent;
}

/** 带条件跳作业列表：后端 `/api/jobs?status=` 支持状态筛选，日期是 UTC 日界原样传递 */
function goJobs(query: Record<string, string>) {
  router.push({ path: "/logharvest/jobs", query });
}

async function load() {
  loading.value = true;
  forbidden.value = false;
  try {
    data.value = await getJobStats({
      days: days.value,
      data_source_id: dataSourceId.value,
    });
  } catch (error) {
    data.value = null;
    if (apiErrorStatus(error) === 403) {
      forbidden.value = true;
    } else {
      message(apiErrorDetail(error, "任务统计加载失败"), { type: "error" });
    }
  } finally {
    loading.value = false;
  }
}

async function loadDataSources() {
  try {
    dataSources.value = await listDataSources();
  } catch (error) {
    dataSources.value = [];
    // 数据源下拉失败不影响统计本身，降级为「不过滤」并说明原因
    message(apiErrorDetail(error, "数据源下拉加载失败，统计不受影响"), {
      type: "warning",
    });
  }
}

const statusQuickJumps: {
  key: keyof NonNullable<typeof totals.value>;
  label: string;
}[] = [
  { key: "running", label: "运行中" },
  { key: "succeeded", label: "成功" },
  { key: "partial", label: "部分成功" },
  { key: "failed", label: "失败" },
  { key: "cancelled", label: "已取消" },
];

const topRuleColumns = computed<TableColumnList>(() => {
  const columns: TableColumnList = [
    // @pureadmin/table 3.3.0 只有列定义声明 slot 才渲染自定义单元格
    {
      label: "规则 ID",
      prop: "rule_id",
      minWidth: 160,
      showOverflowTooltip: true,
    },
    { label: "命中行数", prop: "hits", width: 110 },
    { label: "涉及作业", prop: "jobs", width: 110 },
  ];
  if (!isMobile.value) {
    columns.splice(1, 0, {
      label: "规则版本",
      prop: "rule_version",
      width: 100,
    });
  }
  return columns;
});

onMounted(() => {
  loadDataSources();
  load();
});
</script>

<template>
  <div class="p-4">
    <div class="mb-3 flex flex-wrap items-center gap-3">
      <h2 class="text-lg font-medium">任务统计</h2>
      <el-radio-group v-model="days" :disabled="loading" @change="load">
        <el-radio-button v-for="d in DAYS_OPTIONS" :key="d" :value="d">
          {{ d }} 天
        </el-radio-button>
      </el-radio-group>
      <!-- 宽度挂在包裹层上：el-select 自身是 width:100%，在 flex-wrap 行里会独占一行 -->
      <div class="w-52">
        <el-select
          v-model="dataSourceId"
          clearable
          filterable
          placeholder="全部数据源"
          class="w-full"
          :disabled="loading"
          @change="load"
        >
          <el-option
            v-for="item in dataSources"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
      <span class="text-xs text-gray-500"
        >日期为 UTC 日界，与本地时间可能差一天</span
      >
    </div>

    <el-result
      v-if="forbidden"
      icon="warning"
      title="无查看权限"
      sub-title="任务统计需要 jobs:read 权限（接口返回 403），请联系管理员分配后重试。"
    />

    <template v-else>
      <el-row v-loading="loading" :gutter="12">
        <el-col :xs="24" :sm="12" :lg="6">
          <el-card shadow="never">
            <div class="text-sm text-gray-500">作业数</div>
            <div class="text-2xl font-medium">
              {{ totals ? totals.jobs : "—" }}
            </div>
            <div
              v-if="totals && totals.jobs > 0"
              class="mt-2 flex flex-wrap gap-1"
            >
              <el-tag
                v-for="item in statusQuickJumps"
                :key="item.key"
                :type="statusTagType(item.key)"
                size="small"
                effect="light"
                class="cursor-pointer"
                @click="goJobs({ status: String(item.key) })"
              >
                {{ item.label }} {{ totals[item.key] }}
              </el-tag>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <el-card shadow="never">
            <div class="text-sm text-gray-500">处理字节</div>
            <div class="text-2xl font-medium">
              {{ totals ? formatGB(totals.bytes_total) : "—" }}
            </div>
            <div v-if="totals" class="mt-2 text-xs text-gray-500">
              文件任务共 {{ totals.files_total }} 个
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <el-card shadow="never">
            <div class="text-sm text-gray-500">命中行数</div>
            <div class="text-2xl font-medium">
              {{ totals ? totals.matched_rows_total : "—" }}
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :lg="6">
          <el-card shadow="never">
            <div class="text-sm text-gray-500">文件失败率</div>
            <div class="text-2xl font-medium">
              <span
                v-if="failureRate !== null"
                :class="failureRate > 10 ? 'text-red-600' : 'text-green-700'"
              >
                {{ failureRate.toFixed(1) }}%
              </span>
              <span v-else class="text-gray-400">未知</span>
            </div>
            <div v-if="totals" class="mt-2 text-xs text-gray-500">
              成功 {{ totals.file_succeeded }} / 失败 {{ totals.file_failed }}
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-card v-loading="loading" shadow="never" class="mt-3">
        <template #header
          >每日趋势（{{ days }} 天，条形长度按命中行数归一）</template
        >
        <el-empty
          v-if="isEmpty && !loading"
          description="该时间段没有作业"
          :image-size="70"
        />
        <div v-else-if="daily.length" class="space-y-1">
          <div
            v-for="row in daily"
            :key="row.date"
            class="daily-row cursor-pointer rounded px-2 py-1 hover:bg-gray-50"
            @click="goJobs({ date: row.date })"
          >
            <div class="flex items-center justify-between text-sm">
              <span class="w-24 font-mono">{{ row.date }}</span>
              <span class="hidden text-gray-500 sm:inline">
                作业 {{ row.jobs_created }} · 字节
                {{ formatGB(row.bytes_total) }}
              </span>
              <span class="flex items-center gap-2">
                <el-tag
                  v-if="row.files_succeeded > 0"
                  type="success"
                  size="small"
                  effect="plain"
                  @click.stop="goJobs({ status: 'succeeded', date: row.date })"
                >
                  成 {{ row.files_succeeded }}
                </el-tag>
                <el-tag
                  v-if="row.files_failed > 0"
                  type="danger"
                  size="small"
                  effect="plain"
                  @click.stop="goJobs({ status: 'failed', date: row.date })"
                >
                  败 {{ row.files_failed }}
                </el-tag>
                <span class="w-20 text-right">{{ row.matched_rows }} 行</span>
              </span>
            </div>
            <el-progress
              :percentage="barPercent(row)"
              :stroke-width="10"
              :show-text="false"
              :status="row.files_failed > 0 ? 'warning' : 'success'"
            />
          </div>
          <p class="pt-1 text-xs text-gray-500">
            点击某一行跳「任务创建」列表并带上当日条件；点「成/败」标签额外带上状态条件。
          </p>
        </div>
        <el-empty
          v-else-if="!loading"
          description="该时间段没有作业"
          :image-size="70"
        />
      </el-card>

      <el-card shadow="never" class="mt-3">
        <template #header>Top 规则（按命中行数）</template>
        <pure-table
          v-if="topRules.length || loading"
          row-key="rule_id"
          adaptive
          :size="isMobile ? 'small' : 'default'"
          :loading="loading"
          :data="topRules"
          :columns="topRuleColumns"
        />
        <el-empty v-else description="该时间段没有命中" :image-size="60" />
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.daily-row + .daily-row {
  margin-top: 2px;
}
</style>
