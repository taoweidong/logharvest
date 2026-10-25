<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import { getRuleSet, listRuleSets } from "@/api/logharvestAdmin";
import type { RuleSet } from "@/api/logharvestTypes";
import { formatDateTime } from "../format";
import EditDrawer from "./edit-drawer.vue";

defineOptions({ name: "RuleSets" });

const loading = ref(false);
const rows = ref<RuleSet[]>([]);
/** 列表接口不返回 `config`，规则数逐条从详情补（每页最多 20 次请求） */
const ruleCounts = ref<Record<number, number | null>>({});

const page = reactive({ limit: 20, offset: 0 });
/** 裸数组无 total：取满一页即认为可能存在下一页 */
const hasNext = computed(() => rows.value.length === page.limit);

const drawerVisible = ref(false);
const editingSourceId = ref<number | null>(null);

async function load() {
  loading.value = true;
  try {
    rows.value = await listRuleSets({ limit: page.limit, offset: page.offset });
    ruleCounts.value = {};
    rows.value.forEach(row => {
      getRuleSet(row.id)
        .then(detail => {
          ruleCounts.value[row.id] = detail.config.rules.length;
        })
        .catch(() => {
          ruleCounts.value[row.id] = null;
        });
    });
  } catch (error) {
    rows.value = [];
    message(apiErrorDetail(error, "规则集列表加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingSourceId.value = null;
  drawerVisible.value = true;
}

function openVersionBase(row: RuleSet) {
  editingSourceId.value = row.id;
  drawerVisible.value = true;
}

const columns: TableColumnList = [
  { label: "ID", prop: "id", width: 90 },
  { label: "名称", prop: "name", minWidth: 200, showOverflowTooltip: true },
  { label: "版本", prop: "version", width: 90 },
  { label: "规则数", prop: "rule_count", width: 100 },
  { label: "创建时间", prop: "created_at", width: 190 },
  { label: "操作", prop: "operation", width: 180, fixed: "right" }
];

onMounted(load);
</script>

<template>
  <div class="p-4">
    <PureTableBar title="规则集" :columns="columns" @refresh="load">
      <template #buttons>
        <el-button type="primary" @click="openCreate">新建规则集</el-button>
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
          <template #rule_count="{ row }">
            <el-tag v-if="ruleCounts[row.id] != null" :size="size" effect="plain">
              {{ ruleCounts[row.id] }}
            </el-tag>
            <span v-else class="text-gray-400">加载中…</span>
          </template>
          <template #created_at="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
          <template #operation="{ row }">
            <el-button
              :size="size"
              link
              type="primary"
              @click="openVersionBase(row)"
            >
              查看/另存新版本
            </el-button>
          </template>
        </pure-table>
        <div class="mt-3 flex items-center justify-between text-sm">
          <span class="text-gray-500">
            后端列表接口不返回总数与规则内容；同名规则集以版本递增共存，第
            {{ Math.floor(page.offset / page.limit) + 1 }} 页
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

    <EditDrawer v-model="drawerVisible" :source-id="editingSourceId" @created="load" />
  </div>
</template>
