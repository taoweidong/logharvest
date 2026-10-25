<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import type { FormInstance, FormRules } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import { createDataSource, listDataSources, testDataSource } from "@/api/logharvestAdmin";
import type { DataSource, DataSourceTestResult } from "@/api/logharvestTypes";
import { formatDateTime } from "../format";

defineOptions({ name: "DataSources" });

const loading = ref(false);
const rows = ref<DataSource[]>([]);
/** 每行“测试路径”的最近一次结果，按数据源 id 存 */
const testResults = ref<Record<number, DataSourceTestResult>>({});
const testing = ref<number | null>(null);

const page = reactive({ limit: 50, offset: 0 });
const hasNext = computed(() => rows.value.length === page.limit);

const createVisible = ref(false);
const creating = ref(false);
const formRef = ref<FormInstance>();
const createForm = reactive({
  name: "",
  root_path: "",
  description: ""
});
const createRules: FormRules = {
  name: [{ required: true, message: "请填写数据源名称", trigger: "blur" }],
  root_path: [{ required: true, message: "请填写挂载点绝对路径", trigger: "blur" }]
};

async function load() {
  loading.value = true;
  try {
    rows.value = await listDataSources({ limit: page.limit, offset: page.offset });
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
    message(apiErrorDetail(error, "路径测试失败"), { type: "error" });
  } finally {
    testing.value = null;
  }
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
      description: createForm.description
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

const columns: TableColumnList = [
  { label: "ID", prop: "id", width: 80 },
  { label: "名称", prop: "name", minWidth: 160, showOverflowTooltip: true },
  { label: "根路径", prop: "root_path", minWidth: 240, showOverflowTooltip: true },
  { label: "只读", prop: "read_only", slot: "read_only", width: 90 },
  { label: "启用", prop: "enabled", slot: "enabled", width: 90 },
  { label: "创建时间", prop: "created_at", slot: "created_at", width: 190 },
  { label: "路径测试", prop: "test", slot: "test", minWidth: 260 },
  { label: "操作", prop: "operation", slot: "operation", width: 120, fixed: "right" }
];

onMounted(load);
</script>

<template>
  <div class="p-4">
    <PureTableBar title="数据源" :columns="columns" @refresh="load">
      <template #buttons>
        <el-button type="primary" @click="createVisible = true">
          新建数据源
        </el-button>
      </template>
      <template v-slot="{ size }">
        <pure-table
          :size="size"
          row-key="id"
          adaptive
          :loading="loading"
          :data="rows"
          :columns="columns"
        >
          <template #read_only="{ row }">
            <el-tag :size="size" :type="row.read_only ? 'success' : 'danger'" effect="plain">
              {{ row.read_only ? "是" : "否" }}
            </el-tag>
          </template>
          <template #enabled="{ row }">
            <el-tag :size="size" :type="row.enabled ? 'success' : 'info'" effect="plain">
              {{ row.enabled ? "启用" : "停用" }}
            </el-tag>
          </template>
          <template #created_at="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
          <template #test="{ row }">
            <div v-if="testResults[row.id]" class="flex gap-1">
              <el-tag
                v-for="flag in (['exists', 'is_dir', 'readable'] as const)"
                :key="flag"
                :size="size"
                :type="testResults[row.id][flag] ? 'success' : 'danger'"
                effect="plain"
              >
                {{ flag }}: {{ testResults[row.id][flag] ? "✓" : "✗" }}
              </el-tag>
            </div>
            <span v-else class="text-gray-400">未测试</span>
          </template>
          <template #operation="{ row }">
            <el-button
              :size="size"
              link
              type="primary"
              :loading="testing === row.id"
              @click="onTest(row)"
            >
              测试路径
            </el-button>
          </template>
        </pure-table>
        <div class="mt-3 flex items-center justify-between text-sm">
          <span class="text-gray-500">
            后端列表接口不返回总数；第 {{ Math.floor(page.offset / page.limit) + 1 }}
            页，本页 {{ rows.length }} 条
          </span>
          <div class="flex gap-2">
            <el-button :disabled="page.offset === 0 || loading" @click="(page.offset -= page.limit), load()">
              上一页
            </el-button>
            <el-button :disabled="!hasNext || loading" @click="(page.offset += page.limit), load()">
              下一页
            </el-button>
          </div>
        </div>
      </template>
    </PureTableBar>

    <el-dialog
      v-model="createVisible"
      title="新建数据源"
      width="560px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form ref="formRef" :model="createForm" :rules="createRules" label-width="100px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="createForm.name" clearable placeholder="唯一名称，重名返回 409" />
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
          <el-input v-model="createForm.description" type="textarea" :rows="2" />
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
