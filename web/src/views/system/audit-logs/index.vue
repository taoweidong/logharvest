<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import dayjs from "dayjs";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { message } from "@/utils/message";
import { apiErrorDetail, apiErrorStatus } from "@/api/logharvest";
import { formatDateTime, toApiDateTime } from "@/views/logharvest/format";
import { listOperationLogs } from "@/api/audit";
import {
  OPERATION_ACTION_FAMILIES,
  OPERATION_RESOURCE_TYPES,
  type OperationLog,
} from "@/api/auditTypes";
import { listUsers } from "@/api/rbac";
import type { UserRecord } from "@/api/rbacTypes";
import { useAppStoreHook } from "@/store/modules/app";
import { ADAPTIVE_OFFSET_BOTTOM, TABLE_HEADER_CELL_STYLE } from "../table";

import Refresh from "~icons/ep/refresh";
import View from "~icons/ep/view";

defineOptions({ name: "AuditLogs" });

const loading = ref(false);
const rows = ref<OperationLog[]>([]);
/** 接口 403（无 audit:read）时整页降级提示；菜单侧虽已隐藏，直达 URL 仍会进来 */
const forbidden = ref(false);
const loadError = ref("");

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

/** 后端列表是裸数组无 total，沿用窗口式 limit/offset（后端 limit 上限 500） */
const page = reactive({ limit: 50, offset: 0 });
const hasNext = computed(() => rows.value.length === page.limit);
const pageIndex = computed(() => Math.floor(page.offset / page.limit) + 1);

const form = reactive({
  /** datetimerange 一次性给出成对时间，天然满足后端「必须成对」的 422 契约 */
  range: null as [Date, Date] | null,
  actor_id: undefined as number | undefined,
  action: "",
  resource_type: "",
  resource_id: "",
});

const users = ref<UserRecord[]>([]);
/** /api/users 可能因权限不足失败：下拉降级为不可用，不影响日志查询本身 */
const usersFailed = ref(false);

async function loadUsers() {
  try {
    users.value = await listUsers({ limit: 500, offset: 0 });
    usersFailed.value = false;
  } catch {
    users.value = [];
    usersFailed.value = true;
  }
}

const rangeOk = computed(
  () => !form.range || (!!form.range[0] && !!form.range[1]),
);

async function load() {
  if (!rangeOk.value) {
    message("时间范围必须成对给出，否则后端返回 422", { type: "warning" });
    return;
  }
  loading.value = true;
  forbidden.value = false;
  loadError.value = "";
  try {
    rows.value = await listOperationLogs({
      limit: page.limit,
      offset: page.offset,
      actor_id: form.actor_id,
      action: form.action || undefined,
      resource_type: form.resource_type || undefined,
      resource_id: form.resource_id.trim() || undefined,
      created_from: form.range ? toApiDateTime(form.range[0]) : undefined,
      created_to: form.range ? toApiDateTime(form.range[1]) : undefined,
    });
  } catch (error) {
    rows.value = [];
    if (apiErrorStatus(error) === 403) {
      forbidden.value = true;
    } else {
      loadError.value = apiErrorDetail(error, "操作日志加载失败");
      message(loadError.value, { type: "error" });
    }
  } finally {
    loading.value = false;
  }
}

function onSearch() {
  page.offset = 0;
  load();
}

function onReset() {
  form.range = null;
  form.actor_id = undefined;
  form.action = "";
  form.resource_type = "";
  form.resource_id = "";
  onSearch();
}

function goPrev() {
  page.offset = Math.max(0, page.offset - page.limit);
  load();
}

function goNext() {
  page.offset += page.limit;
  load();
}

/** 设计 §11：用户名可跳「该用户近 7 天」——就地改筛选并重新查询 */
function filterByActor(row: OperationLog) {
  if (row.actor_id === null) return;
  form.actor_id = row.actor_id;
  form.range = [dayjs().subtract(7, "day").toDate(), new Date()];
  onSearch();
}

// ---------------------------------------------------------------- 详情抽屉

const detailVisible = ref(false);
const detailRow = ref<OperationLog | null>(null);

/** `detail` 列是任意 JSON；展示前统一格式化缩进，空值给明确文案 */
const detailJson = computed(() => {
  const value = detailRow.value?.detail;
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
});

function openDetail(row: OperationLog) {
  detailRow.value = row;
  detailVisible.value = true;
}

const columns = computed<TableColumnList>(() => {
  const all: TableColumnList = [
    // @pureadmin/table 3.3.0 只有列定义声明 slot 才渲染自定义单元格
    { label: "时间", prop: "created_at", slot: "created_at", width: 170 },
    { label: "操作人", prop: "actor_name", slot: "actor_name", minWidth: 150 },
    { label: "动作", prop: "action", minWidth: 150, showOverflowTooltip: true },
    { label: "对象", prop: "resource_type", slot: "resource", minWidth: 140 },
    {
      label: "摘要",
      prop: "summary",
      minWidth: 220,
      showOverflowTooltip: true,
    },
    { label: "来源 IP", prop: "origin_ip", width: 130 },
    {
      label: "操作",
      prop: "operation",
      slot: "operation",
      width: 80,
      fixed: isMobile.value ? undefined : "right",
    },
  ];
  if (isMobile.value) {
    // 窄屏裁掉摘要与 IP，保留「谁在什么时候做了什么」三要素
    return all.filter(
      (item) => !["摘要", "来源 IP"].includes(String(item.label ?? "")),
    );
  }
  return all;
});

onMounted(() => {
  loadUsers();
  load();
});
</script>

<template>
  <div class="p-4">
    <el-result
      v-if="forbidden"
      icon="warning"
      title="无查看权限"
      sub-title="操作日志需要 audit:read 权限（接口返回 403）。该权限默认只授予管理员与操作员，只读值守角色不可见。"
    />

    <template v-else>
      <!-- 六个筛选条件放在表格上方独立一行：塞进 PureTableBar 的 #buttons 会在
           中等宽度下换行，换行部分与表格抢位置，查询/重置会点不到。
           宽度写在包裹的 div 上：Element Plus 的 select/input/datepicker 根节点
           自带 `width:100%`，直接给组件加 `w-36` 会被它盖掉而撑满整行。 -->
      <el-form
        :inline="true"
        :model="form"
        class="search-form bg-bg_color mb-2"
        @submit.prevent
      >
        <el-form-item label="时间范围：">
          <div class="w-[400px] max-w-full">
            <el-date-picker
              v-model="form.range"
              type="datetimerange"
              start-placeholder="开始时间"
              end-placeholder="结束时间"
              class="!w-full"
              unlink-panels
            />
          </div>
        </el-form-item>
        <el-form-item label="操作人：">
          <div class="w-36">
            <el-select
              v-model="form.actor_id"
              clearable
              filterable
              :disabled="usersFailed"
              :placeholder="usersFailed ? '用户列表不可用' : '操作人'"
              class="w-full"
            >
              <el-option
                v-for="item in users"
                :key="item.id"
                :label="item.username"
                :value="item.id"
              />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="动作族：">
          <div class="w-36">
            <el-select
              v-model="form.action"
              clearable
              placeholder="动作族"
              class="w-full"
            >
              <el-option
                v-for="item in OPERATION_ACTION_FAMILIES"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="对象类型：">
          <div class="w-32">
            <el-select
              v-model="form.resource_type"
              clearable
              placeholder="对象类型"
              class="w-full"
            >
              <el-option
                v-for="item in OPERATION_RESOURCE_TYPES"
                :key="item"
                :label="item"
                :value="item"
              />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="对象 ID：">
          <div class="w-28">
            <el-input
              v-model="form.resource_id"
              clearable
              placeholder="对象 ID"
              class="w-full"
              @keyup.enter="onSearch"
            />
          </div>
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :icon="useRenderIcon('ri/search-line')"
            :loading="loading"
            @click="onSearch"
          >
            查询
          </el-button>
          <el-button :icon="useRenderIcon(Refresh)" :disabled="loading" @click="onReset">
            重置
          </el-button>
        </el-form-item>
      </el-form>
      <p class="mb-2 text-xs text-gray-500">
        时间为浏览器本地时区显示；服务端按 UTC 存储，列表不含总数、按时间倒序
      </p>

      <PureTableBar title="操作日志" :columns="columns" @refresh="load">
        <template v-slot="{ size, dynamicColumns }">
          <pure-table
            row-key="id"
            adaptive
            :adaptiveConfig="{ offsetBottom: ADAPTIVE_OFFSET_BOTTOM }"
            align-whole="center"
            showOverflowTooltip
            table-layout="auto"
            :size="size"
            :loading="loading"
            :data="rows"
            :columns="dynamicColumns"
            :header-cell-style="TABLE_HEADER_CELL_STYLE"
          >
            <template #created_at="{ row }">
              {{ formatDateTime(row.created_at) }}
            </template>
            <template #actor_name="{ row }">
              <el-button
                v-if="row.actor_id !== null"
                :size="size"
                link
                type="primary"
                @click="filterByActor(row)"
              >
                {{ row.actor_name }}
              </el-button>
              <span v-else>{{ row.actor_name }}（账号已删除）</span>
              <div
                v-if="row.role_hint && !isMobile"
                class="text-xs text-gray-400"
              >
                {{ row.role_hint }}
              </div>
            </template>
            <template #resource="{ row }">
              <span v-if="row.resource_type">
                {{ row.resource_type
                }}<template v-if="row.resource_id">
                  #{{ row.resource_id }}</template
                >
              </span>
              <span v-else class="text-gray-400">—</span>
            </template>
            <template #operation="{ row }">
              <el-button
                class="reset-margin"
                :size="size"
                link
                type="primary"
                :icon="useRenderIcon(View)"
                @click="openDetail(row)"
              >
                详情
              </el-button>
            </template>
          </pure-table>
          <el-empty
            v-if="!rows.length && !loading"
            :description="loadError || '尚未产生写操作'"
            :image-size="70"
          />
          <div v-else class="mt-3 flex items-center justify-between text-sm">
            <span class="text-gray-500">
              第 {{ pageIndex }} 页，本页 {{ rows.length }} 条
            </span>
            <div class="flex gap-2">
              <el-button
                :disabled="page.offset === 0 || loading"
                @click="goPrev"
              >
                上一页
              </el-button>
              <el-button :disabled="!hasNext || loading" @click="goNext">
                下一页
              </el-button>
            </div>
          </div>
        </template>
      </PureTableBar>

      <el-drawer
        v-model="detailVisible"
        title="操作详情"
        :size="isMobile ? '92%' : 'min(560px, 92vw)'"
        append-to-body
      >
        <template v-if="detailRow">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="ID">{{
              detailRow.id
            }}</el-descriptions-item>
            <el-descriptions-item label="时间">
              {{ formatDateTime(detailRow.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="操作人">
              {{ detailRow.actor_name }}（{{
                detailRow.role_hint || "无角色快照"
              }}）
            </el-descriptions-item>
            <el-descriptions-item label="动作">{{
              detailRow.action
            }}</el-descriptions-item>
            <el-descriptions-item label="对象">
              {{ detailRow.resource_type || "—" }}
              {{ detailRow.resource_id ? `#${detailRow.resource_id}` : "" }}
            </el-descriptions-item>
            <el-descriptions-item label="摘要">{{
              detailRow.summary
            }}</el-descriptions-item>
            <el-descriptions-item label="来源 IP">{{
              detailRow.origin_ip
            }}</el-descriptions-item>
          </el-descriptions>
          <div class="mt-3 text-sm font-medium">
            detail（变更前后/参数摘要）
          </div>
          <pre
            v-if="detailJson"
            class="mt-1 overflow-auto rounded bg-gray-50 p-3 text-xs leading-6 dark:bg-gray-800"
            >{{ detailJson }}</pre>
          <p v-else class="mt-1 text-sm text-gray-400">该记录没有附加信息</p>
        </template>
      </el-drawer>
    </template>
  </div>
</template>
