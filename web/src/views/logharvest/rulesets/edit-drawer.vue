<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import { useRouter } from "vue-router";
import { getRuleSet } from "@/api/logharvestAdmin";
import type { Rule, RuleSetDetail } from "@/api/logharvestTypes";
import { formatDateTime, formatDetailError } from "../format";
import { hasPerms } from "@/utils/auth";
import { TABLE_HEADER_CELL_STYLE } from "@/config/table";

defineOptions({ name: "RuleSetDetailDrawer" });

const visible = defineModel<boolean>({ default: false });
const props = defineProps<{
  /** 要查看的规则集版本 ID */
  sourceId?: number | null;
}>();

const router = useRouter();

/**
 * 只读用户只看概览，不给任何写入口；「去维护页编辑」按钮只对 `rulesets:write` 可见，
 * 规则的增删改与另存新版本全部在 `/rules/maintenance` 整页完成（规格 §9）。
 */
const canWrite = computed(() => hasPerms("rulesets:write"));

const state = reactive({
  loading: false,
  error: "",
  detail: null as RuleSetDetail | null
});

/** 匹配方式的一行摘要：抽屉只做概览，不做编辑 */
function matchSummary(rule: Rule): string {
  if (rule.match.type === "keyword") {
    const side = rule.match.all.length ? "全部命中" : "任一命中";
    const words = (rule.match.all.length ? rule.match.all : rule.match.any).join("、");
    return `关键字（${side}，${rule.match.case_sensitive ? "区分大小写" : "忽略大小写"}）：${words}`;
  }
  const flags = rule.match.flags || "无";
  return `正则 /${rule.match.pattern}/（标志 ${flags}，超时 ${rule.match.timeout_ms}ms）`;
}

/** 阈值摘要：只有正则命名分组才可能有阈值，后端负责校验 */
function thresholdSummary(rule: Rule): string {
  if (!rule.threshold) return "-";
  const { field, op, value } = rule.threshold;
  return `${field} ${op} ${value}`;
}

const ruleColumns = computed(() => [
  // @pureadmin/table 3.3.0 只解析列定义里的 `slot`，所以带定制的列都要显式写出来。
  { label: "规则 id", prop: "id", minWidth: 140, showOverflowTooltip: true },
  { label: "展示名", prop: "name", minWidth: 140, showOverflowTooltip: true },
  { label: "严重级", prop: "severity", width: 100 },
  { label: "启用", prop: "enabled", slot: "enabled", width: 80 },
  { label: "匹配", prop: "match", slot: "match", minWidth: 260 },
  { label: "阈值", prop: "threshold", slot: "threshold", minWidth: 150 },
  { label: "标签", prop: "tags", slot: "tags", minWidth: 140 }
]);

watch(visible, async open => {
  if (!open) return;
  state.error = "";
  state.detail = null;
  if (props.sourceId == null) return;
  state.loading = true;
  try {
    state.detail = await getRuleSet(props.sourceId);
  } catch (error) {
    state.error = formatDetailError(error, "规则集详情加载失败");
  } finally {
    state.loading = false;
  }
});

function goMaintenance() {
  if (!canWrite.value || state.detail === null) return;
  router.push({
    path: "/rules/maintenance",
    query: {
      rule_set_id: String(state.detail.id),
      version: String(state.detail.version)
    }
  });
  visible.value = false;
}
</script>

<template>
  <el-drawer
    v-model="visible"
    :title="
      state.detail
        ? `规则集 ${state.detail.name} v${state.detail.version}（#${state.detail.id}）`
        : `查看规则集${sourceId != null ? ` #${sourceId}` : ''}`
    "
    size="min(860px, 94vw)"
    :close-on-click-modal="false"
  >
    <div v-loading="state.loading" style="min-height: 8rem">
      <el-alert
        v-if="state.error"
        type="error"
        :closable="false"
        show-icon
        class="mb-3"
        title="详情加载失败"
      >
        <pre class="whitespace-pre-wrap">{{ state.error }}</pre>
      </el-alert>

      <template v-else-if="state.detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="ID">#{{ state.detail.id }}</el-descriptions-item>
          <el-descriptions-item label="名称">{{ state.detail.name }}</el-descriptions-item>
          <el-descriptions-item label="版本">v{{ state.detail.version }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ state.detail.created_at }}
          </el-descriptions-item>
          <el-descriptions-item label="创建人">
            {{ state.detail.created_by || "-" }}
          </el-descriptions-item>
          <el-descriptions-item label="规则条数">
            {{ state.detail.config.rules.length }}
          </el-descriptions-item>
          <el-descriptions-item label="文件通配">
            {{ state.detail.config.file_glob }}
          </el-descriptions-item>
          <el-descriptions-item label="编码">
            {{ state.detail.config.encoding }} / 回退
            {{ state.detail.config.fallback_encoding }}
          </el-descriptions-item>
        </el-descriptions>

        <pure-table
          class="mt-3"
          size="small"
          row-key="id"
          :border="true"
          :data="state.detail.config.rules"
          :columns="ruleColumns"
          :header-cell-style="TABLE_HEADER_CELL_STYLE"
        >
          <template #enabled="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">
              {{ row.enabled ? "启用" : "停用" }}
            </el-tag>
          </template>
          <template #match="{ row }">
            <span class="break-all text-xs">{{ matchSummary(row) }}</span>
          </template>
          <template #threshold="{ row }">
            <span class="text-xs">{{ thresholdSummary(row) }}</span>
          </template>
          <template #tags="{ row }">
            <span class="text-xs">{{ row.tags.length ? row.tags.join(", ") : "-" }}</span>
          </template>
        </pure-table>
      </template>

      <el-empty v-else description="未选择规则集版本" :image-size="64" />
    </div>

    <template #footer>
      <div class="flex flex-wrap items-center justify-between gap-3">
        <span class="page-hint">
          {{
            canWrite
              ? "规则编辑已迁到「规则维护」整页：改完另存为新版本（同名版本 max+1）"
              : "当前账号只有只读权限，界面不提供任何写入口"
          }}
        </span>
        <div class="page-foot__actions">
          <el-button @click="visible = false">关闭</el-button>
          <el-button
            v-if="canWrite"
            v-perms="'rulesets:write'"
            type="primary"
            :disabled="state.detail === null"
            @click="goMaintenance"
          >
            去维护页编辑
          </el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>
