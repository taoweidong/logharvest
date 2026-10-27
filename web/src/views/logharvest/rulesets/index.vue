<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import { getRuleSet, listRuleSets } from "@/api/logharvestAdmin";
import type { RuleSet } from "@/api/logharvestTypes";
import { formatDateTime } from "../format";
import { useAppStoreHook } from "@/store/modules/app";
import EditDrawer from "./edit-drawer.vue";

defineOptions({ name: "RuleSets" });

const router = useRouter();
const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const loading = ref(false);
const rows = ref<RuleSet[]>([]);
/** 列表接口不返回 `config`，规则数逐条从详情补（每页最多 20 次请求） */
const ruleCounts = ref<Record<number, number | null>>({});

const page = reactive({ limit: 20, offset: 0 });
/** 裸数组无 total：取满一页即认为可能存在下一页 */
const hasNext = computed(() => rows.value.length === page.limit);

const drawerVisible = ref(false);
const detailId = ref<number | null>(null);

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

function openDetail(row: RuleSet) {
  detailId.value = row.id;
  drawerVisible.value = true;
}

/**
 * 规则的重活（增删改、另存新版本）在「规则维护」页做：那里是整页表格，
 * 比抽屉里塞一套完整编辑器更好用，也避免同一个编辑表单在两处各写一份。
 */
function goMaintenance(row?: RuleSet) {
  router.push({
    path: "/rules/maintenance",
    query: row
      ? { rule_set_id: String(row.id), version: String(row.version) }
      : undefined
  });
}

const columns = computed<TableColumnList>(() => [
  { label: "ID", prop: "id", width: isMobile.value ? 70 : 90 },
  {
    label: "名称",
    prop: "name",
    minWidth: isMobile.value ? 160 : 200,
    showOverflowTooltip: true
  },
  { label: "版本", prop: "version", width: 90 },
  { label: "规则数", prop: "rule_count", slot: "rule_count", width: 100 },
  ...(isMobile.value
    ? []
    : ([
        { label: "创建时间", prop: "created_at", slot: "created_at", width: 190 }
      ] as TableColumnList)),
  {
    label: "操作",
    prop: "operation",
    slot: "operation",
    width: isMobile.value ? 130 : 200,
    fixed: isMobile.value ? undefined : "right"
  }
]);

onMounted(load);
</script>

<template>
  <div class="p-4">
    <PureTableBar title="规则集" :columns="columns" @refresh="load">
      <template #buttons>
        <div class="flex flex-wrap items-center justify-end gap-2">
          <el-button v-perms="'rulesets:write'" @click="goMaintenance()">
            新建规则集
          </el-button>
        </div>
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
            <el-button :size="size" link type="primary" @click="openDetail(row)">
              详情
            </el-button>
            <el-button
              v-perms="'rulesets:write'"
              :size="size"
              link
              type="primary"
              @click="goMaintenance(row)"
            >
              编辑规则
            </el-button>
          </template>
        </pure-table>
        <div class="mt-3 flex flex-wrap items-center justify-between gap-2 text-sm">
          <span class="text-gray-500">
            后端列表接口不返回总数与规则内容；同名规则集以版本递增共存，第
            {{ Math.floor(page.offset / page.limit) + 1 }} 页。规则的增删改与「另存新版本」在
            「规则管理 → 规则维护」整页完成。
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

    <EditDrawer v-model="drawerVisible" :source-id="detailId" />
  </div>
</template>
