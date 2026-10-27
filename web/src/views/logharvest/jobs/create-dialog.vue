<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import type { FormInstance, FormRules } from "element-plus";
import { message } from "@/utils/message";
import { apiErrorDetail, createJob } from "@/api/logharvest";
import {
  getRuleSet,
  listDataSources,
  listRuleSets,
  testDataSource
} from "@/api/logharvestAdmin";
import type {
  DataSource,
  DataSourceTestResult,
  Job,
  RuleSet
} from "@/api/logharvestTypes";

defineOptions({ name: "JobCreateDialog" });

const visible = defineModel<boolean>({ default: false });
const props = defineProps<{
  /** 数据源页「用它建任务」带过来的 ID，在第①步预填 */
  prefillDataSourceId?: number | null;
}>();
const emit = defineEmits<{ (e: "created", job: Job): void }>();

const router = useRouter();

/** 向导三步：0 数据源与目录 → 1 规则集与版本 → 2 文件过滤与确认 */
const step = ref(0);
const submitting = ref(false);
const step0Ref = ref<FormInstance>();
const step1Ref = ref<FormInstance>();

const dataSources = ref<DataSource[]>([]);
const ruleSets = ref<RuleSet[]>([]);
/** 列表接口不返回 `config`，规则条数只能逐条取详情，取过的缓存在这里 */
const ruleCounts = ref<Record<number, number | null>>({});
const optionsLoaded = ref(false);
const optionsLoading = ref(false);

/** 第①步的路径判定结果（测的是数据源 root_path，不是本次扫描目录） */
const testing = ref(false);
const testResult = ref<DataSourceTestResult | null>(null);
const testError = ref("");

/** 后端原文错误（尤其 422）直接摊在界面上，不做二次加工 */
const backendError = ref("");

const form = reactive({
  name: "",
  data_source_id: undefined as number | undefined,
  source_path: "",
  rule_set_id: undefined as number | undefined,
  recursive: true,
  max_depth: 5,
  suffixesText: "",
  max_files: 20000
});

/**
 * 同一次提交的失败重试必须复用同一个 `Idempotency-Key`（后端按
 * `(created_by, idempotency_key)` 回放已有作业；键相同但请求体不同会 409），
 * 所以键只在“打开对话框 / 提交成功 / 表单内容变化”时重新生成。
 */
let idempotencyKey = crypto.randomUUID();

const step0Rules: FormRules = {
  name: [{ required: true, message: "请填写作业名称", trigger: "blur" }],
  data_source_id: [{ required: true, message: "请选择数据源", trigger: "change" }],
  source_path: [{ required: true, message: "请填写要扫描的目录", trigger: "blur" }]
};

const step1Rules: FormRules = {
  rule_set_id: [{ required: true, message: "请选择规则集版本", trigger: "change" }]
};

const selectedSource = computed(
  () => dataSources.value.find(item => item.id === form.data_source_id) ?? null
);

const selectedRuleSet = computed(
  () => ruleSets.value.find(item => item.id === form.rule_set_id) ?? null
);

const selectedRuleCount = computed(() =>
  form.rule_set_id ? ruleCounts.value[form.rule_set_id] : undefined
);

/** 扫描目录的约束提示：必须落在所选数据源的 root_path 之内 */
const selectedRootHint = computed(() =>
  selectedSource.value
    ? `必须位于 ${selectedSource.value.root_path} 之内，且是已存在的目录；符号链接与点目录会被跳过`
    : "请先选择数据源"
);

const suffixes = computed(() =>
  form.suffixesText
    .split(/[\s,，]+/)
    .map(item => item.trim())
    .filter(Boolean)
);

/** 条数区分「还没取」与「取失败」，两者不能都写成“待加载” */
const ruleCountText = computed(() => {
  if (!form.rule_set_id) return "未选择";
  const count = selectedRuleCount.value;
  if (typeof count === "number") return `${count} 条`;
  return count === null ? "条数获取失败（不阻断提交）" : "加载中…";
});

const summaryLines = computed(() => [
  `数据源：${selectedSource.value ? `${selectedSource.value.name}（#${selectedSource.value.id}）` : "-"}`,
  `扫描目录：${form.source_path || "-"}`,
  `规则集：${
    selectedRuleSet.value
      ? `${selectedRuleSet.value.name} v${selectedRuleSet.value.version}（#${
          selectedRuleSet.value.id
        }，${ruleCountText.value}）`
      : "-"
  }`,
  `文件过滤：递归=${form.recursive ? "是" : "否"}，深度=${form.max_depth}，上限=${
    form.max_files
  }${suffixes.value.length ? `，后缀=${suffixes.value.join(" ")}` : "，后缀=后端默认全部压缩包"}`
]);

async function loadOptions() {
  if (optionsLoading.value) return;
  optionsLoading.value = true;
  try {
    const [sources, sets] = await Promise.all([
      listDataSources(),
      listRuleSets({ limit: 200, offset: 0 })
    ]);
    dataSources.value = sources;
    ruleSets.value = sets;
    optionsLoaded.value = true;
  } catch (error) {
    message(apiErrorDetail(error, "数据源/规则集列表加载失败"), { type: "error" });
  } finally {
    optionsLoading.value = false;
  }
}

/** 规则条数按需取：只在选中某个版本时才请求它的详情 */
async function ensureRuleCount(id: number | undefined) {
  if (id === undefined || ruleCounts.value[id] !== undefined) return;
  try {
    const detail = await getRuleSet(id);
    ruleCounts.value[id] = detail.config.rules.length;
  } catch {
    // 条数只是辅助信息，取不到不阻断建任务；界面显示“规则条数待加载”
    ruleCounts.value[id] = null;
  }
}

watch(
  () => form.rule_set_id,
  id => void ensureRuleCount(id)
);

watch(visible, open => {
  if (!open) return;
  step.value = 0;
  backendError.value = "";
  testResult.value = null;
  testError.value = "";
  idempotencyKey = crypto.randomUUID();
  if (props.prefillDataSourceId != null) {
    form.data_source_id = props.prefillDataSourceId;
  }
  void loadOptions();
});

// 表单内容变了就换键，避免拿着旧键提交新内容被后端判为 409
watch(form, () => {
  idempotencyKey = crypto.randomUUID();
});

/** 三态只读判定：与后端 schema 的不变量一致，`unknown` 绝不显示成“已确认只读” */
function readonlyVerdict(result: DataSourceTestResult) {
  if (result.writability === "writable") {
    return { text: "可写：后端会拒绝处理（409）", type: "danger" as const };
  }
  if (result.read_only_confirmed) {
    return { text: "只读已确认", type: "success" as const };
  }
  return { text: "只读未确认（平台判不出来）", type: "warning" as const };
}

async function onTestPath() {
  if (form.data_source_id === undefined) return;
  testing.value = true;
  testResult.value = null;
  testError.value = "";
  try {
    testResult.value = await testDataSource(form.data_source_id);
  } catch (error) {
    testError.value = apiErrorDetail(error, "路径测试失败");
  } finally {
    testing.value = false;
  }
}

/**
 * 逐步推进：只校验当前步骤的表单，失败就停在原地。
 * 校验器返回 `false` 时 Element Plus 会把字段错误显示出来，这里不再额外弹提示。
 */
async function onNext() {
  backendError.value = "";
  if (step.value === 0) {
    const valid = await step0Ref.value?.validate().catch(() => false);
    if (!valid) return;
    step.value = 1;
    return;
  }
  if (step.value === 1) {
    const valid = await step1Ref.value?.validate().catch(() => false);
    if (!valid) return;
    await ensureRuleCount(form.rule_set_id);
    step.value = 2;
  }
}

function onPrev() {
  backendError.value = "";
  step.value = Math.max(0, step.value - 1);
}

/** 后端按状态码给不同提示，其余文案直接用 `detail` 原文 */
function failHint(status: number | undefined, detail: string): string {
  if (status === 400) return `路径不合法：${detail}`;
  if (status === 409) return `幂等冲突或目录不可用：请求内容已变更（${detail}）`;
  if (status === 422) return `参数校验失败：${detail}`;
  if (status === 503) return `目录扫描超过 10 秒：${detail}`;
  return detail;
}

async function onSubmit() {
  submitting.value = true;
  backendError.value = "";
  try {
    const job = await createJob(
      {
        name: form.name.trim(),
        data_source_id: form.data_source_id!,
        source_path: form.source_path.trim(),
        rule_set_id: form.rule_set_id!,
        file_filter: {
          recursive: form.recursive,
          max_depth: form.max_depth,
          max_files: form.max_files,
          // 留空传 null：交给后端用它支持的压缩包后缀集合
          suffixes: suffixes.value.length ? suffixes.value : null
        }
      },
      idempotencyKey
    );
    message(`作业 #${job.id} 已创建，共 ${job.total_files} 个压缩文件`, {
      type: "success"
    });
    idempotencyKey = crypto.randomUUID();
    emit("created", job);
    visible.value = false;
  } catch (error: any) {
    backendError.value = failHint(
      error?.response?.status,
      apiErrorDetail(error, "创建失败")
    );
  } finally {
    submitting.value = false;
  }
}

function goDataSources() {
  visible.value = false;
  router.push("/logharvest/datasources");
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="新建分析任务"
    width="min(680px, 94vw)"
    :close-on-click-modal="false"
    append-to-body
  >
    <el-steps :active="step" align-center finish-status="success" class="mb-4">
      <el-step title="数据源与目录" />
      <el-step title="规则集" />
      <el-step title="过滤与确认" />
    </el-steps>

    <el-form
      v-show="step === 0"
      ref="step0Ref"
      :model="form"
      :rules="step0Rules"
      label-width="100px"
    >
      <el-form-item label="作业名称" prop="name">
        <el-input v-model="form.name" clearable placeholder="例如：web01 一周错误日志" />
      </el-form-item>
      <el-form-item label="数据源" prop="data_source_id">
        <el-select v-model="form.data_source_id" class="w-full" placeholder="选择挂载点">
          <el-option
            v-for="item in dataSources"
            :key="item.id"
            :label="`${item.name}（${item.root_path}）${item.enabled ? '' : ' 已停用'}`"
            :value="item.id"
          />
        </el-select>
        <div v-if="selectedSource && !selectedSource.enabled" class="mt-1 text-xs text-red-500">
          该数据源已停用，后端提交时会返回 404（data source not found or disabled）
        </div>
      </el-form-item>
      <el-form-item label="扫描目录" prop="source_path">
        <el-input
          v-model="form.source_path"
          clearable
          placeholder="数据源 root_path 之内的绝对路径"
        />
        <div class="mt-1 text-xs text-gray-500">
          {{
            selectedRootHint
          }}
        </div>
      </el-form-item>
      <el-form-item label="路径判定">
        <div class="flex flex-wrap items-center gap-2">
          <el-button
            :loading="testing"
            :disabled="form.data_source_id === undefined"
            @click="onTestPath"
          >
            测试数据源根路径
          </el-button>
          <span class="text-xs text-gray-500">
            后端只校验数据源的 root_path（符号链接、点目录会被跳过）
          </span>
        </div>
        <div v-if="testResult" class="mt-2 flex flex-wrap gap-1">
          <el-tag
            v-for="flag in (['exists', 'is_dir', 'readable'] as const)"
            :key="flag"
            size="small"
            :type="testResult[flag] ? 'success' : 'danger'"
            effect="plain"
          >
            {{ { exists: "存在", is_dir: "目录", readable: "可读" }[flag] }}:
            {{ testResult[flag] ? "✓" : "✗" }}
          </el-tag>
          <el-tag size="small" :type="readonlyVerdict(testResult).type" effect="plain">
            {{ readonlyVerdict(testResult).text }}
          </el-tag>
        </div>
        <div v-else-if="testError" class="mt-2 text-xs text-red-500">
          {{ testError }}
        </div>
      </el-form-item>
      <el-alert
        v-if="optionsLoaded && dataSources.length === 0"
        type="warning"
        :closable="false"
        show-icon
        title="还没有任何数据源"
      >
        <template #default>
          <span class="text-xs">
            需要先登记一个企业磁盘挂载点。
            <el-button link type="primary" @click="goDataSources">去建数据源</el-button>
          </span>
        </template>
      </el-alert>
    </el-form>

    <el-form
      v-show="step === 1"
      ref="step1Ref"
      :model="form"
      :rules="step1Rules"
      label-width="100px"
    >
      <el-form-item label="规则集版本" prop="rule_set_id">
        <el-select v-model="form.rule_set_id" filterable class="w-full">
          <el-option
            v-for="item in ruleSets"
            :key="item.id"
            :label="`${item.name} v${item.version}（#${item.id}）`"
            :value="item.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="规则条数">
        <el-tag v-if="typeof selectedRuleCount === 'number'" effect="plain">
          {{ selectedRuleCount }} 条
        </el-tag>
        <span v-else class="text-xs text-gray-500">
          {{ ruleCountText }}（列表接口不返回规则内容，选中后才取详情）
        </span>
      </el-form-item>
    </el-form>

    <div v-show="step === 2">
      <el-form :model="form" label-width="100px">
        <el-form-item label="递归扫描">
          <el-switch v-model="form.recursive" />
        </el-form-item>
        <el-form-item label="最大深度">
          <el-input-number v-model="form.max_depth" :min="1" :max="20" />
          <span class="ml-2 text-xs text-gray-500">1–20，达到该深度的目录不再下探</span>
        </el-form-item>
        <el-form-item label="文件后缀">
          <el-input
            v-model="form.suffixesText"
            clearable
            placeholder="留空=后端默认的全部压缩包后缀，例如 .gz .tar.gz"
          />
          <div class="mt-1 text-xs text-gray-500">
            只接受后端支持的压缩包后缀（.zip、.tar/.tar.gz/.tar.bz2/.tar.xz/.tgz/.tbz2/.txz、
            .gz/.bz2/.xz）；填了却没有一个合法就返回 400，原文会列出国支持集合。
          </div>
        </el-form-item>
        <el-form-item label="最大文件数">
          <el-input-number v-model="form.max_files" :min="1" :max="20000" :step="1000" />
          <span class="ml-2 text-xs text-gray-500">超出后后端立即中止扫描并返回 400</span>
        </el-form-item>
      </el-form>

      <el-descriptions :column="1" border size="small" title="提交摘要">
        <el-descriptions-item v-for="(line, index) in summaryLines" :key="index">
          {{ line }}
        </el-descriptions-item>
      </el-descriptions>
    </div>

    <el-alert
      v-if="backendError"
      class="mt-3"
      type="error"
      :closable="false"
      show-icon
      title="提交未成功"
    >
      <pre class="whitespace-pre-wrap">{{ backendError }}</pre>
    </el-alert>

    <template #footer>
      <div class="flex items-center justify-between gap-2">
        <span class="text-xs text-gray-500">
          {{ step + 1 }} / 3 · 提交后直接进入作业详情并跟随实时进度
        </span>
        <div class="flex gap-2">
          <el-button @click="visible = false">取消</el-button>
          <el-button v-show="step > 0" @click="onPrev">上一步</el-button>
          <el-button v-show="step < 2" type="primary" @click="onNext">下一步</el-button>
          <el-button
            v-show="step === 2"
            type="primary"
            :loading="submitting"
            @click="onSubmit"
          >
            提交
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>
