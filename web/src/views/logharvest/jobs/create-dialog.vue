<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import type { FormInstance, FormRules } from "element-plus";
import { message } from "@/utils/message";
import { apiErrorDetail, createJob } from "@/api/logharvest";
import { listDataSources, listRuleSets } from "@/api/logharvestAdmin";
import type { DataSource, Job, RuleSet } from "@/api/logharvestTypes";

defineOptions({ name: "JobCreateDialog" });

const visible = defineModel<boolean>({ default: false });
const emit = defineEmits<{ (e: "created", job: Job): void }>();

const formRef = ref<FormInstance>();
const submitting = ref(false);
const dataSources = ref<DataSource[]>([]);
const ruleSets = ref<RuleSet[]>([]);
const optionsLoaded = ref(false);

/**
 * 同一次提交的失败重试必须复用同一个 `Idempotency-Key`（后端按
 * `(created_by, idempotency_key)` 回放已有作业；键相同但请求体不同会 409），
 * 所以键只在“打开对话框 / 提交成功 / 表单内容变化”时重新生成。
 */
let idempotencyKey = crypto.randomUUID();

const form = reactive({
  name: "",
  data_source_id: undefined as number | undefined,
  source_path: "",
  rule_set_id: undefined as number | undefined,
  recursive: true,
  max_depth: 5,
  max_files: 20000
});

const rules: FormRules = {
  name: [{ required: true, message: "请填写作业名称", trigger: "blur" }],
  data_source_id: [{ required: true, message: "请选择数据源", trigger: "change" }],
  source_path: [{ required: true, message: "请填写要扫描的目录", trigger: "blur" }],
  rule_set_id: [{ required: true, message: "请选择规则集", trigger: "change" }]
};

const selectedRoot = computed(
  () =>
    dataSources.value.find(item => item.id === form.data_source_id)?.root_path ?? ""
);

async function loadOptions() {
  if (optionsLoaded.value) return;
  try {
    const [sources, sets] = await Promise.all([listDataSources(), listRuleSets()]);
    dataSources.value = sources;
    ruleSets.value = sets;
    optionsLoaded.value = true;
  } catch (error) {
    message(apiErrorDetail(error, "数据源/规则集列表加载失败"), { type: "error" });
  }
}

watch(visible, open => {
  if (open) {
    idempotencyKey = crypto.randomUUID();
    loadOptions();
  }
});

// 表单内容变了就换键，避免拿着旧键提交新内容被后端判为 409
watch(form, () => {
  idempotencyKey = crypto.randomUUID();
});

/** 后端按状态码给不同提示，其余文案直接用 `detail` 原文 */
function failHint(status: number | undefined, detail: string): string {
  if (status === 400) return `路径不合法：${detail}`;
  if (status === 409) return `幂等冲突：请求内容已变更（${detail}）`;
  if (status === 422) return `参数校验失败：${detail}`;
  if (status === 503) return `目录扫描超过 10 秒：${detail}`;
  return detail;
}

async function onSubmit() {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
  submitting.value = true;
  try {
    const job = await createJob(
      {
        name: form.name,
        data_source_id: form.data_source_id!,
        source_path: form.source_path,
        rule_set_id: form.rule_set_id!,
        file_filter: {
          recursive: form.recursive,
          max_depth: form.max_depth,
          max_files: form.max_files
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
    message(failHint(error?.response?.status, apiErrorDetail(error)), {
      type: "error"
    });
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="新建分析作业"
    width="620px"
    :close-on-click-modal="false"
    append-to-body
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="120px">
      <el-form-item label="作业名称" prop="name">
        <el-input v-model="form.name" clearable placeholder="例如：web01 一周错误日志" />
      </el-form-item>
      <el-form-item label="数据源" prop="data_source_id">
        <el-select v-model="form.data_source_id" class="w-full" placeholder="选择挂载点">
          <el-option
            v-for="item in dataSources"
            :key="item.id"
            :label="`${item.name}（${item.root_path}）`"
            :value="item.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="扫描目录" prop="source_path">
        <el-input
          v-model="form.source_path"
          clearable
          placeholder="数据源 root_path 之内的绝对路径"
        />
        <div class="mt-1 text-xs text-gray-500">
          {{
            selectedRoot
              ? `必须位于 ${selectedRoot} 之内，且是已存在的目录；符号链接与点目录会被跳过`
              : "请先选择数据源"
          }}
        </div>
      </el-form-item>
      <el-form-item label="规则集" prop="rule_set_id">
        <el-select v-model="form.rule_set_id" class="w-full" placeholder="选择规则集版本">
          <el-option
            v-for="item in ruleSets"
            :key="item.id"
            :label="`${item.name} v${item.version}（#${item.id}）`"
            :value="item.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="递归扫描">
        <el-switch v-model="form.recursive" />
      </el-form-item>
      <el-form-item label="最大深度">
        <el-input-number v-model="form.max_depth" :min="1" :max="20" />
        <span class="ml-2 text-xs text-gray-500">1–20，达到该深度的目录不再下探（静默截断）</span>
      </el-form-item>
      <el-form-item label="最大文件数">
        <el-input-number v-model="form.max_files" :min="1" :max="20000" :step="1000" />
        <span class="ml-2 text-xs text-gray-500">1–20000，超出后后端立即中止扫描并返回 400</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="onSubmit">
        提交
      </el-button>
    </template>
  </el-dialog>
</template>
