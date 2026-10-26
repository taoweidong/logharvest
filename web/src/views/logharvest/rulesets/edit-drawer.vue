<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import type { FormInstance, FormRules } from "element-plus";
import { message } from "@/utils/message";
import { createRuleSet, getRuleSet } from "@/api/logharvestAdmin";
import type {
  KeywordMatch,
  RegexMatch,
  Rule,
  RuleSet,
  RuleSetCreatePayload,
  RuleSetDetail
} from "@/api/logharvestTypes";
import { formatDetailError } from "../format";
import { hasPerms } from "@/utils/auth";

defineOptions({ name: "RuleSetEditDrawer" });

const visible = defineModel<boolean>({ default: false });
const props = defineProps<{
  /** 传入已有规则集 ID 时预填其配置（另存为新版本）；为空则是全新规则集 */
  sourceId?: number | null;
}>();
const emit = defineEmits<{ (e: "created", ruleSet: RuleSet): void }>();

/** 只读用户仍可查看配置（列表操作叫“查看/另存新版本”），但不能提交新版本。 */
const canWrite = computed(() => hasPerms("rulesets:write"));

/** 表单内的规则行：把后端的判别联合摊平成可编辑字段，提交时再组装回 `match` */
type RuleForm = {
  id: string;
  name: string;
  severity: string;
  enabled: boolean;
  tagsText: string;
  matchType: "keyword" | "regex";
  /** keyword.any 与 keyword.all 互斥，用单选决定关键词写进哪一侧 */
  kwMode: "any" | "all";
  keywordsText: string;
  caseSensitive: boolean;
  pattern: string;
  flagI: boolean;
  flagM: boolean;
  flagS: boolean;
  timeoutMs: number;
  useThreshold: boolean;
  thresholdField: string;
  thresholdOp: "gt" | "gte" | "lt" | "lte" | "eq";
  thresholdValue: string;
};

const newRule = (): RuleForm => ({
  id: "",
  name: "",
  severity: "info",
  enabled: true,
  tagsText: "",
  matchType: "keyword",
  kwMode: "any",
  keywordsText: "",
  caseSensitive: false,
  pattern: "",
  flagI: false,
  flagM: false,
  flagS: false,
  timeoutMs: 50,
  useThreshold: false,
  thresholdField: "",
  thresholdOp: "gt",
  thresholdValue: ""
});

const formRef = ref<FormInstance>();
const submitting = ref(false);
const loadingSource = ref(false);
const backendError = ref("");

const form = reactive({
  name: "",
  file_glob: "**/*",
  encoding: "utf-8",
  fallback_encoding: "gb18030",
  rules: [newRule()] as RuleForm[]
});

const rules: FormRules = {
  name: [{ required: true, message: "请填写规则集名称", trigger: "blur" }],
  file_glob: [{ required: true, message: "请填写文件通配符", trigger: "blur" }]
};

const SEVERITIES = ["info", "warning", "error"];
const THRESHOLD_OPS = ["gt", "gte", "lt", "lte", "eq"] as const;

function splitList(text: string): string[] {
  return text
    .split(/[\n,，]/)
    .map(item => item.trim())
    .filter(Boolean);
}

function toRule(row: RuleForm): Rule {
  const keywords = splitList(row.keywordsText);
  const match: KeywordMatch | RegexMatch =
    row.matchType === "keyword"
      ? {
          type: "keyword",
          any: row.kwMode === "any" ? keywords : [],
          all: row.kwMode === "all" ? keywords : [],
          case_sensitive: row.caseSensitive
        }
      : {
          type: "regex",
          pattern: row.pattern,
          flags: `${row.flagI ? "i" : ""}${row.flagM ? "m" : ""}${row.flagS ? "s" : ""}`,
          timeout_ms: row.timeoutMs
        };
  return {
    id: row.id.trim(),
    name: row.name,
    enabled: row.enabled,
    severity: row.severity,
    tags: splitList(row.tagsText),
    match,
    threshold: row.useThreshold
      ? {
          field: row.thresholdField.trim(),
          op: row.thresholdOp,
          // 后端字段是 Decimal，字符串形式避免精度丢失
          value: row.thresholdValue.trim()
        }
      : null
  };
}

/** 提交前的最小客户端校验（互斥/必填/唯一），其余交给后端 422 原文 */
function validateRows(): string | null {
  if (!form.rules.length) return "至少需要一条规则";
  const ids = form.rules.map(row => row.id.trim());
  if (ids.some(id => !id)) return "每条规则都要填 id";
  if (new Set(ids).size !== ids.length) return "规则 id 不能重复";
  for (const row of form.rules) {
    if (row.matchType === "keyword" && !splitList(row.keywordsText).length)
      return `规则 ${row.id}：关键词（${row.kwMode === "any" ? "任一命中" : "全部命中"}）不能为空`;
    if (row.matchType === "regex" && !row.pattern.trim())
      return `规则 ${row.id}：正则模式不能为空`;
    if (row.timeoutMs <= 0) return `规则 ${row.id}：超时须大于 0 毫秒`;
    if (row.useThreshold && row.matchType === "keyword")
      return `规则 ${row.id}：阈值依赖正则命名分组，关键字规则没有可提取字段`;
    if (row.useThreshold && !row.thresholdField.trim())
      return `规则 ${row.id}：请填写阈值的字段名（正则命名分组）`;
  }
  return null;
}

async function fillFrom(source: RuleSetDetail) {
  form.name = source.config.name || source.name;
  form.file_glob = source.config.file_glob;
  form.encoding = source.config.encoding;
  form.fallback_encoding = source.config.fallback_encoding;
  form.rules = source.config.rules.map(row => {
    const base = newRule();
    base.id = row.id;
    base.name = row.name;
    base.severity = row.severity;
    base.enabled = row.enabled;
    base.tagsText = row.tags.join(", ");
    if (row.match.type === "keyword") {
      base.matchType = "keyword";
      base.kwMode = row.match.all.length ? "all" : "any";
      base.keywordsText = (row.match.all.length
        ? row.match.all
        : row.match.any
      ).join("\n");
      base.caseSensitive = row.match.case_sensitive;
    } else {
      base.matchType = "regex";
      base.pattern = row.match.pattern;
      base.flagI = row.match.flags.includes("i");
      base.flagM = row.match.flags.includes("m");
      base.flagS = row.match.flags.includes("s");
      base.timeoutMs = row.match.timeout_ms;
    }
    if (row.threshold) {
      base.useThreshold = true;
      base.thresholdField = row.threshold.field;
      base.thresholdOp = row.threshold.op;
      base.thresholdValue = String(row.threshold.value);
    }
    return base;
  });
}

watch(visible, async open => {
  if (!open) return;
  backendError.value = "";
  if (props.sourceId == null) return;
  loadingSource.value = true;
  try {
    const detail = await getRuleSet(props.sourceId);
    await fillFrom(detail);
  } catch (error) {
    backendError.value = formatDetailError(error, "规则集详情加载失败");
  } finally {
    loadingSource.value = false;
  }
});

async function onSubmit() {
  if (!canWrite.value) return;
  backendError.value = "";
  const invalid = validateRows();
  if (invalid) {
    backendError.value = invalid;
    return;
  }
  if (formRef.value) {
    const valid = await formRef.value.validate().catch(() => false);
    if (!valid) return;
  }
  submitting.value = true;
  try {
    const payload: RuleSetCreatePayload = {
      name: form.name.trim(),
      config: {
        name: form.name.trim(),
        file_glob: form.file_glob,
        encoding: form.encoding,
        fallback_encoding: form.fallback_encoding,
        rules: form.rules.map(toRule)
      }
    };
    const created = await createRuleSet(payload);
    message(`已创建“${created.name}”版本 ${created.version}`, {
      type: "success"
    });
    emit("created", created);
    visible.value = false;
  } catch (error) {
    // 后端已做正则编译/pathspec/codec/命名分组校验，这里原样转述 422/409
    backendError.value = formatDetailError(error, "保存失败");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <el-drawer
    v-model="visible"
    :title="
      !canWrite
        ? `查看规则集${sourceId != null ? ` #${sourceId}` : ''}`
        : sourceId == null
          ? '新建规则集'
          : `基于规则集 #${sourceId} 另存新版本（后端同名版本自动 +1）`
    "
    size="760px"
    :close-on-click-modal="false"
  >
    <el-form
      ref="formRef"
      v-loading="loadingSource"
      :model="form"
      :rules="rules"
      :disabled="!canWrite"
      label-width="110px"
    >
      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" clearable placeholder="规则集名称" />
      </el-form-item>
      <el-form-item label="文件通配" prop="file_glob">
        <el-input v-model="form.file_glob" placeholder="**/*" />
        <div class="hint">
          对压缩包内成员路径生效的 pathspec（后端编译校验，非法会返回 422）
        </div>
      </el-form-item>
      <el-form-item label="编码">
        <el-input v-model="form.encoding" class="w-40" />
      </el-form-item>
      <el-form-item label="回退编码">
        <el-input v-model="form.fallback_encoding" class="w-40" />
      </el-form-item>

      <el-divider content-position="left">
        规则（{{ form.rules.length }} 条）
      </el-divider>
      <el-card
        v-for="(row, index) in form.rules"
        :key="index"
        class="mb-3"
        shadow="never"
      >
        <template #header>
          <div class="flex items-center justify-between">
            <span>规则 {{ row.id || index + 1 }}</span>
            <el-button
              v-if="form.rules.length > 1"
              link
              type="danger"
              @click="form.rules.splice(index, 1)"
            >
              删除
            </el-button>
          </div>
        </template>
        <el-form-item label="规则 id" required>
          <el-input v-model="row.id" placeholder="稳定标识，命中结果里回显" />
        </el-form-item>
        <el-form-item label="展示名">
          <el-input v-model="row.name" />
        </el-form-item>
        <el-form-item label="严重级">
          <el-select v-model="row.severity" class="w-40">
            <el-option v-for="s in SEVERITIES" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="row.tagsText" placeholder="逗号或换行分隔" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="row.enabled" />
        </el-form-item>
        <el-form-item label="匹配方式">
          <el-radio-group v-model="row.matchType">
            <el-radio-button value="keyword">关键字</el-radio-button>
            <el-radio-button value="regex">正则</el-radio-button>
          </el-radio-group>
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
            <el-checkbox v-model="row.flagI">i（忽略大小写）</el-checkbox>
            <el-checkbox v-model="row.flagM">m（多行）</el-checkbox>
            <el-checkbox v-model="row.flagS">s（点匹配换行）</el-checkbox>
            <div class="hint">后端仅允许 i/m/s 三个标志</div>
          </el-form-item>
          <el-form-item label="超时（毫秒）" required>
            <el-input-number v-model="row.timeoutMs" :min="1" :step="50" />
            <span class="ml-2 text-xs text-gray-500">必须大于 0，防灾难性回溯</span>
          </el-form-item>
        </template>
        <el-form-item label="数值阈值">
          <el-switch v-model="row.useThreshold" />
          <div v-if="row.useThreshold" class="mt-2 w-full">
            <div class="flex flex-wrap items-center gap-2">
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
              字段必须是上面正则里的命名分组，是否成立由后端校验（422 原文回显）
            </div>
          </div>
        </el-form-item>
      </el-card>
      <el-button v-if="canWrite" class="mb-3" @click="form.rules.push(newRule())">
        添加规则
      </el-button>

      <el-alert
        v-if="backendError"
        type="error"
        :closable="false"
        show-icon
        class="mb-3"
      >
        <pre class="whitespace-pre-wrap">{{ backendError }}</pre>
      </el-alert>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ canWrite ? "取消" : "关闭" }}</el-button>
      <el-button
        v-if="canWrite"
        type="primary"
        :loading="submitting"
        @click="onSubmit"
      >
        {{ sourceId == null ? "创建" : "另存为新版本" }}
      </el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
</style>
