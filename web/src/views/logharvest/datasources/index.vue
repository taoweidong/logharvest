<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import type { FormInstance, FormRules } from "element-plus";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import {
  createDataSource,
  listDataSources,
  testDataSource,
} from "@/api/logharvestAdmin";
import type { DataSource, DataSourceTestResult } from "@/api/logharvestTypes";
import { formatDateTime } from "../format";

defineOptions({ name: "DataSources" });

const router = useRouter();

const loading = ref(false);
const rows = ref<DataSource[]>([]);
/** 每个数据源“测试路径”的最近一次结果，按 id 存（后端没有查询历史接口） */
const testResults = ref<Record<number, DataSourceTestResult>>({});
const testing = ref<number | null>(null);

/** 后端列表接口是裸数组且没有 total：窗口式 limit/offset 翻页 */
const page = reactive({ limit: 50, offset: 0 });
const hasNext = computed(() => rows.value.length === page.limit);
const pageIndex = computed(() => Math.floor(page.offset / page.limit) + 1);

const createVisible = ref(false);
const creating = ref(false);
const formRef = ref<FormInstance>();
const createForm = reactive({
  name: "",
  root_path: "",
  description: "",
});
const createRules: FormRules = {
  name: [{ required: true, message: "请填写数据源名称", trigger: "blur" }],
  root_path: [
    { required: true, message: "请填写挂载点绝对路径", trigger: "blur" },
  ],
};

async function load() {
  loading.value = true;
  try {
    rows.value = await listDataSources({
      limit: page.limit,
      offset: page.offset,
    });
  } catch (error) {
    rows.value = [];
    message(apiErrorDetail(error, "数据源列表加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

async function onTest(row: DataSource) {
  testing.value = row.id;
  try {
    testResults.value[row.id] = await testDataSource(row.id);
  } catch (error) {
    // 测试失败就把上一次结果丢掉：留着旧徽章会被读成“这次也通过了”
    delete testResults.value[row.id];
    message(apiErrorDetail(error, "路径测试失败"), { type: "error" });
  } finally {
    testing.value = null;
  }
}

/** 路径测试布尔项的中文化：不要直接把后端的字段名摆在界面上 */
const TEST_FLAG_LABELS: Record<"exists" | "is_dir" | "readable", string> = {
  exists: "存在",
  is_dir: "目录",
  readable: "可读",
};

/**
 * 只读判定的展示。与后端 schema 的不变量保持一致：只有 `read_only_confirmed` 为真才
 * 显示"已确认只读"。判不出来（Windows / SMB 挂载，后端返回 `unknown` + `false`）时
 * 必须如实说"未确认"——给用户一个假的绿勾比承认判定不了更糟（规格 §7.2）。
 */
function readonlyVerdict(result: DataSourceTestResult) {
  if (result.writability === "writable") {
    return { text: "可写：禁止处理", type: "danger" as const };
  }
  if (result.read_only_confirmed) {
    return { text: "只读已确认", type: "success" as const };
  }
  return { text: "只读未确认", type: "warning" as const };
}

async function onCreate() {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
  creating.value = true;
  try {
    const created = await createDataSource({
      name: createForm.name.trim(),
      root_path: createForm.root_path.trim(),
      // 后端 `read_only` 是 Literal[True]，只接受 true，界面不提供开关
      read_only: true,
      description: createForm.description,
    });
    message(`数据源“${created.name}”已创建`, { type: "success" });
    createVisible.value = false;
    createForm.name = "";
    createForm.root_path = "";
    createForm.description = "";
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "创建失败"), { type: "error" });
  } finally {
    creating.value = false;
  }
}

/** 带上 data_source_id 跳任务页，创建向导第①步会预填 */
function useForJob(row: DataSource) {
  router.push({
    path: "/logharvest/jobs",
    query: { data_source_id: String(row.id) },
  });
}

onMounted(load);
</script>

<template>
  <div class="page">
    <section class="page-panel">
      <div class="page-panel__head">
        <p class="page-panel__title">
          数据源
          <span class="page-hint font-normal">
            企业磁盘挂载点登记；后端强制只读，处理时不会回写源目录
          </span>
        </p>
        <div class="page-toolbar__end">
          <el-button :disabled="loading" @click="load">刷新</el-button>
          <el-button
            v-perms="'datasources:write'"
            type="primary"
            @click="createVisible = true"
          >
            新建数据源
          </el-button>
        </div>
      </div>
    </section>

    <section class="page-panel page-panel--grow">
      <div v-loading="loading" class="page-body">
        <div v-if="rows.length" class="page-grid">
          <el-card v-for="row in rows" :key="row.id" shadow="never">
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0">
                <div class="truncate text-sm font-medium">
                  {{ row.name }}
                  <span class="page-hint">#{{ row.id }}</span>
                </div>
                <div
                  class="mt-1 break-all font-mono text-xs text-[var(--el-text-color-secondary)]"
                >
                  {{ row.root_path }}
                </div>
              </div>
              <div class="flex shrink-0 flex-col items-end gap-1">
                <el-tag
                  :type="row.enabled ? 'success' : 'info'"
                  size="small"
                  effect="plain"
                >
                  {{ row.enabled ? "启用" : "停用" }}
                </el-tag>
                <el-tag
                  :type="row.read_only ? 'success' : 'danger'"
                  size="small"
                  effect="plain"
                >
                  登记只读：{{ row.read_only ? "是" : "否" }}
                </el-tag>
              </div>
            </div>

            <p v-if="row.description" class="page-hint mt-2">
              {{ row.description }}
            </p>

            <div class="mt-2 min-h-[1.75rem]">
              <div v-if="testResults[row.id]" class="flex flex-wrap gap-1">
                <el-tag
                  v-for="flag in ['exists', 'is_dir', 'readable'] as const"
                  :key="flag"
                  size="small"
                  :type="testResults[row.id][flag] ? 'success' : 'danger'"
                  effect="plain"
                >
                  {{ TEST_FLAG_LABELS[flag] }}:
                  {{ testResults[row.id][flag] ? "✓" : "✗" }}
                </el-tag>
                <el-tag
                  size="small"
                  :type="readonlyVerdict(testResults[row.id]).type"
                  effect="plain"
                >
                  {{ readonlyVerdict(testResults[row.id]).text }}
                </el-tag>
              </div>
              <span v-else class="page-hint">路径未测试</span>
            </div>

            <div class="mt-3 flex items-center justify-between">
              <span class="page-hint">{{
                formatDateTime(row.created_at)
              }}</span>
              <div class="flex gap-2">
                <el-button
                  link
                  type="primary"
                  :loading="testing === row.id"
                  @click="onTest(row)"
                >
                  测试路径
                </el-button>
                <el-button
                  v-perms="'jobs:write'"
                  link
                  type="primary"
                  @click="useForJob(row)"
                >
                  用它建任务
                </el-button>
              </div>
            </div>
          </el-card>
        </div>

        <el-empty
          v-else-if="!loading"
          description="还没有登记任何数据源：先新建一个挂载点"
          class="page-empty"
        />
      </div>

      <div class="page-foot">
        <span>
          后端列表接口不返回总数；第 {{ pageIndex }} 页，本页
          {{ rows.length }} 条
        </span>
        <div class="page-foot__actions">
          <el-button
            :disabled="page.offset === 0 || loading"
            @click="((page.offset -= page.limit), load())"
          >
            上一页
          </el-button>
          <el-button
            :disabled="!hasNext || loading"
            @click="((page.offset += page.limit), load())"
          >
            下一页
          </el-button>
        </div>
      </div>
    </section>

    <el-dialog
      v-model="createVisible"
      title="新建数据源"
      width="min(560px, 92vw)"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form
        ref="formRef"
        :model="createForm"
        :rules="createRules"
        label-width="100px"
      >
        <el-form-item label="名称" prop="name">
          <el-input
            v-model="createForm.name"
            clearable
            placeholder="唯一名称，重名返回 409"
          />
        </el-form-item>
        <el-form-item label="根路径" prop="root_path">
          <el-input
            v-model="createForm.root_path"
            clearable
            placeholder="企业磁盘挂载点的服务器绝对路径"
          />
        </el-form-item>
        <el-form-item label="只读">
          <el-input model-value="true（后端强制只读，不提供开关）" disabled />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="2"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onCreate">
          创建
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
