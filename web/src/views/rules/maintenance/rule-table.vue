<script setup lang="ts">
import { ElMessageBox } from "element-plus";
import {
  rowError,
  SEVERITIES,
  THRESHOLD_OPS,
  splitList,
  type RuleForm,
} from "./rule-form";

defineOptions({ name: "RuleTable" });

const props = defineProps<{
  rows: RuleForm[];
  readonly: boolean;
}>();

const emit = defineEmits<{
  (e: "add"): void;
  (e: "remove", index: number): void;
  (e: "move", index: number, delta: number): void;
}>();

/** 校验失败的行整行标红（设计 §11「校验失败逐行标红」） */
function rowClassName({ row }: { row: RuleForm }) {
  return rowError(row) ? "rule-row-error" : "";
}

/** el-table 插槽的 row 是宽化的 DefaultRow，进入本组件函数前统一断言回 RuleForm */
function asRule(value: unknown): RuleForm {
  return value as RuleForm;
}

function rowHint(value: unknown): string | null {
  return rowError(asRule(value));
}

function matchSummary(value: unknown): string {
  const row = asRule(value);
  if (row.matchType === "keyword") {
    const words = splitList(row.keywordsText);
    return `${row.kwMode === "any" ? "任一" : "全部"}命中 ${words.length} 词：${words.join("、") || "（空）"}`;
  }
  const flags = `${row.flagI ? "i" : ""}${row.flagM ? "m" : ""}${row.flagS ? "s" : ""}`;
  return `/${row.pattern || "(空)"}/${flags} · ${row.timeoutMs}ms`;
}

function thresholdSummary(value: unknown): string {
  const row = asRule(value);
  if (!row.useThreshold) return "—";
  return `${row.thresholdField} ${row.thresholdOp} ${row.thresholdValue || "?"}`;
}

async function confirmRemove(value: unknown, index: number) {
  const row = asRule(value);
  try {
    await ElMessageBox.confirm(
      `确认删除规则“${row.id || index + 1}”？保存新版本后才会落库。`,
      "删除规则",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  emit("remove", index);
}
</script>

<template>
  <div>
    <el-table
      :data="props.rows"
      :row-class-name="rowClassName"
      height="100%"
      border
      size="small"
      class="w-full"
    >
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="p-4">
            <!-- el-form-item 必须有 el-form 父级：否则拿不到 label-width，
                 展开面板的标签与缩进和整页其它表单对不齐。disabled 沿用整页只读态，
                 否则这层新表单会把外层 el-form 的禁用上下文遮住。 -->
            <el-form label-width="110px" :disabled="props.readonly">
              <div
                v-if="rowHint(row)"
                class="mb-2 text-sm text-[var(--el-color-danger)]"
              >
                {{ rowHint(row) }}
              </div>
              <div class="page-form">
                <el-form-item label="规则 id" required>
                  <el-input
                    v-model="row.id"
                    placeholder="稳定标识，命中结果里回显"
                  />
                </el-form-item>
                <el-form-item label="展示名">
                  <el-input v-model="row.name" />
                </el-form-item>
                <el-form-item label="标签">
                  <el-input
                    v-model="row.tagsText"
                    placeholder="逗号或换行分隔"
                  />
                </el-form-item>
                <template v-if="row.matchType === 'keyword'">
                  <el-form-item label="any/all 互斥">
                    <el-radio-group v-model="row.kwMode">
                      <el-radio value="any">任一命中（any）</el-radio>
                      <el-radio value="all">全部命中（all）</el-radio>
                    </el-radio-group>
                  </el-form-item>
                  <el-form-item label="关键词" required>
                    <el-input
                      v-model="row.keywordsText"
                      type="textarea"
                      :rows="3"
                      placeholder="每行一个（或逗号分隔）；后端要求 any/all 恰好提供一侧"
                    />
                  </el-form-item>
                  <el-form-item label="区分大小写">
                    <el-switch v-model="row.caseSensitive" />
                  </el-form-item>
                </template>
                <template v-else>
                  <el-form-item label="正则模式" required>
                    <el-input
                      v-model="row.pattern"
                      type="textarea"
                      :rows="2"
                      placeholder="Python 语法，命名分组写 (?P&lt;name&gt;...)"
                    />
                  </el-form-item>
                  <el-form-item label="标志位">
                    <el-checkbox v-model="row.flagI"
                      >i（忽略大小写）</el-checkbox
                    >
                    <el-checkbox v-model="row.flagM">m（多行）</el-checkbox>
                    <el-checkbox v-model="row.flagS"
                      >s（点匹配换行）</el-checkbox
                    >
                    <div class="hint">后端仅允许 i/m/s 三个标志</div>
                  </el-form-item>
                  <el-form-item label="超时（毫秒）" required>
                    <el-input-number
                      v-model="row.timeoutMs"
                      :min="1"
                      :step="50"
                    />
                    <span class="page-hint ml-2">必须大于 0，防灾难性回溯</span>
                  </el-form-item>
                </template>
                <el-form-item label="数值阈值">
                  <el-switch v-model="row.useThreshold" />
                  <div v-if="row.useThreshold" class="mt-2 w-full">
                    <div class="page-toolbar">
                      <el-input
                        v-model="row.thresholdField"
                        class="w-44"
                        placeholder="字段（正则命名分组）"
                      />
                      <el-select v-model="row.thresholdOp" class="w-28">
                        <el-option
                          v-for="op in THRESHOLD_OPS"
                          :key="op"
                          :label="op"
                          :value="op"
                        />
                      </el-select>
                      <el-input
                        v-model="row.thresholdValue"
                        class="w-36"
                        placeholder="比较值（数值）"
                      />
                    </div>
                    <div class="hint">
                      字段必须是正则里的命名分组，是否成立由后端校验（422
                      原文回显）； 关键字规则不可启用阈值
                    </div>
                  </div>
                </el-form-item>
              </div>
            </el-form>
          </div>
        </template>
      </el-table-column>
      <el-table-column type="index" label="#" width="46" />
      <el-table-column label="规则 id" min-width="130">
        <template #default="{ row }">
          <el-input v-model="row.id" size="small" placeholder="必填" />
        </template>
      </el-table-column>
      <el-table-column label="严重级" width="110">
        <template #default="{ row }">
          <el-select v-model="row.severity" size="small">
            <el-option v-for="s in SEVERITIES" :key="s" :label="s" :value="s" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70" align="center">
        <template #default="{ row }">
          <el-switch v-model="row.enabled" size="small" />
        </template>
      </el-table-column>
      <el-table-column label="匹配方式" width="150">
        <template #default="{ row }">
          <el-radio-group v-model="row.matchType" size="small">
            <el-radio-button value="keyword">关键字</el-radio-button>
            <el-radio-button value="regex">正则</el-radio-button>
          </el-radio-group>
        </template>
      </el-table-column>
      <el-table-column label="匹配配置" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="text-xs">{{ matchSummary(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="阈值" width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="text-xs">{{ thresholdSummary(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column
        v-if="!props.readonly"
        label="操作"
        width="170"
        align="center"
      >
        <template #default="{ row, $index }">
          <el-button
            link
            type="primary"
            :disabled="$index === 0"
            @click="emit('move', $index, -1)"
          >
            上移
          </el-button>
          <el-button
            link
            type="primary"
            :disabled="$index === props.rows.length - 1"
            @click="emit('move', $index, 1)"
          >
            下移
          </el-button>
          <el-button link type="danger" @click="confirmRemove(row, $index)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-button v-if="!props.readonly" class="mt-3" @click="emit('add')">
      添加规则
    </el-button>
  </div>
</template>

<style scoped>
/* 展开面板里有关键词/正则两段 textarea 与三个阈值控件，layout.scss 默认的
   280px 最小列宽会把它们压得太窄，这里只调这个令牌、不重写 grid。 */
.page-form {
  --pure-form-col-min: 420px;
}

.hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

:deep(.rule-row-error) {
  --el-table-tr-bg-color: var(--el-color-danger-light-9);
}
</style>
