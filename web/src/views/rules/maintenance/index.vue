<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { PureTableBar } from "@/components/RePureTableBar";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import { formatDetailError, severityTagType } from "@/views/logharvest/format";
import { createRuleSet, getRuleSet, listRuleSets } from "@/api/logharvestAdmin";
import { listRuleIndex } from "@/api/analytics";
import type { RuleIndexRow } from "@/api/analyticsTypes";
import type { RuleSet } from "@/api/logharvestTypes";
import { hasPerms } from "@/utils/auth";
import { useAppStoreHook } from "@/store/modules/app";
import RuleTable from "./rule-table.vue";
import {
  fromRule,
  newRule,
  toRule,
  validateRules,
  type RuleForm,
} from "./rule-form";

defineOptions({ name: "RuleMaintenance" });

const route = useRoute();

/** 无 rulesets:write 时整页只读：el-form :disabled + 隐藏保存/行操作，标题变「查看规则」 */
const canWrite = computed(() => hasPerms("rulesets:write"));

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const activeTab = ref<string>("edit");

// ---------------------------------------------------------------- 编辑态

const sets = ref<RuleSet[]>([]);
const loadingSets = ref(false);
const selectedName = ref<string>("");
const selectedId = ref<number | undefined>(undefined);
const loadingDetail = ref(false);
const saving = ref(false);
const backendError = ref("");

const form = reactive({
  name: "",
  file_glob: "**/*",
  encoding: "utf-8",
  fallback_encoding: "gb18030",
  rules: [] as RuleForm[],
});

/** 同名规则集的各版本是独立行；下拉按名字分组、版本倒序 */
const nameOptions = computed(() =>
  [...new Set(sets.value.map((item) => item.name))].sort(),
);
const versionOptions = computed(() =>
  sets.value
    .filter((item) => item.name === selectedName.value)
    .sort((a, b) => b.version - a.version),
);

async function loadSets() {
  loadingSets.value = true;
  try {
    sets.value = await listRuleSets({ limit: 500, offset: 0 });
  } catch (error) {
    sets.value = [];
    message(apiErrorDetail(error, "规则集列表加载失败"), { type: "error" });
  } finally {
    loadingSets.value = false;
  }
}

async function loadVersion(id: number) {
  loadingDetail.value = true;
  backendError.value = "";
  try {
    const detail = await getRuleSet(id);
    form.name = detail.config.name || detail.name;
    // 老库里 config 可能缺 file_glob/encoding 键（实测 dev 数据如此），给默认值兜底，
    // 否则编辑下拉/输入框拿到 undefined，另存版本时也会把缺省写回
    form.file_glob = detail.config.file_glob || "**/*";
    form.encoding = detail.config.encoding || "utf-8";
    form.fallback_encoding = detail.config.fallback_encoding || "gb18030";
    form.rules = detail.config.rules.map(fromRule);
    // 从检索页跳转进来时该版本可能不在已加载列表里，补进下拉保证选中态可见
    if (!sets.value.some((item) => item.id === detail.id)) {
      sets.value.push({
        id: detail.id,
        name: detail.name,
        version: detail.version,
        created_at: detail.created_at,
      });
    }
    selectedName.value = detail.name;
  } catch (error) {
    form.rules = [];
    backendError.value = formatDetailError(error, "规则集版本加载失败");
  } finally {
    loadingDetail.value = false;
  }
}

function onNameChange() {
  const latest = versionOptions.value[0];
  selectedId.value = latest?.id;
  if (latest) loadVersion(latest.id);
}

function onVersionChange(id: number | undefined) {
  if (id != null) loadVersion(id);
}

function onAddRule() {
  form.rules.push(newRule());
}

function onRemoveRule(index: number) {
  form.rules.splice(index, 1);
}

function onMoveRule(index: number, delta: number) {
  const target = index + delta;
  if (target < 0 || target >= form.rules.length) return;
  const rows = form.rules;
  [rows[index], rows[target]] = [rows[target], rows[index]];
}

/** 另存为新版本：复用 POST /api/rule-sets（同名 max+1），成功后留在编辑态可继续改 */
async function onSave() {
  if (!canWrite.value) return;
  backendError.value = "";
  const invalid = validateRules(form.rules);
  if (invalid) {
    backendError.value = invalid;
    return;
  }
  saving.value = true;
  try {
    const created = await createRuleSet({
      name: form.name.trim(),
      config: {
        name: form.name.trim(),
        file_glob: form.file_glob,
        encoding: form.encoding,
        fallback_encoding: form.fallback_encoding,
        rules: form.rules.map(toRule),
      },
    });
    message(`已创建版本 ${created.version}`, { type: "success" });
    await loadSets();
    selectedName.value = created.name;
    selectedId.value = created.id;
  } catch (error) {
    // 后端 422（编译/pathspec/codec/命名分组）原文逐行展示，前端不重复实现校验
    backendError.value = formatDetailError(error, "保存失败");
  } finally {
    saving.value = false;
  }
}

// ---------------------------------------------------------------- 跨版本检索

const search = reactive({
  keyword: "",
  type: "" as "" | "keyword" | "regex",
  rule_set_name: "",
});
/** 后端列表是裸数组无 total，沿用窗口式 limit/offset */
const searchPage = reactive({ limit: 20, offset: 0 });
const searchRows = ref<RuleIndexRow[]>([]);
const searching = ref(false);
const searchSearched = ref(false);
const searchHasNext = computed(
  () => searchRows.value.length === searchPage.limit,
);
const searchPageIndex = computed(
  () => Math.floor(searchPage.offset / searchPage.limit) + 1,
);

async function doSearch() {
  searching.value = true;
  try {
    searchRows.value = await listRuleIndex({
      keyword: search.keyword.trim() || undefined,
      type: search.type || undefined,
      rule_set_name: search.rule_set_name.trim() || undefined,
      limit: searchPage.limit,
      offset: searchPage.offset,
    });
    searchSearched.value = true;
  } catch (error) {
    searchRows.value = [];
    message(apiErrorDetail(error, "规则检索失败"), { type: "error" });
  } finally {
    searching.value = false;
  }
}

function onSearch() {
  searchPage.offset = 0;
  doSearch();
}

function searchPrev() {
  searchPage.offset = Math.max(0, searchPage.offset - searchPage.limit);
  doSearch();
}

function searchNext() {
  searchPage.offset += searchPage.limit;
  doSearch();
}

/** 「去编辑」：跳到编辑 Tab 并直接加载该行的 rule_set_id 版本 */
async function goToEdit(row: RuleIndexRow) {
  activeTab.value = "edit";
  selectedId.value = row.rule_set_id;
  await loadVersion(row.rule_set_id);
}

const searchColumns = computed<TableColumnList>(() => {
  const columns: TableColumnList = [
    // @pureadmin/table 3.3.0 只有列定义声明 slot 才渲染自定义单元格
    {
      label: "规则集",
      prop: "rule_set_name",
      minWidth: 150,
      showOverflowTooltip: true,
    },
    { label: "版本", prop: "version", width: 80 },
    {
      label: "规则 ID",
      prop: "rule_id",
      minWidth: 140,
      showOverflowTooltip: true,
    },
    { label: "类型", prop: "type", slot: "type", width: 90 },
    { label: "严重级", prop: "severity", slot: "severity", width: 100 },
    {
      label: "摘要",
      prop: "summary",
      minWidth: 220,
      showOverflowTooltip: true,
    },
    {
      label: "操作",
      prop: "operation",
      slot: "operation",
      width: 90,
      fixed: isMobile.value ? undefined : "right",
    },
  ];
  if (isMobile.value) {
    // 窄屏裁掉版本与摘要，保住定位规则所需的名称/ID 列
    return columns.filter(
      (item) => !["版本", "摘要"].includes(String(item.label ?? "")),
    );
  }
  return columns;
});

// ---------------------------------------------------------------- 入口

onMounted(async () => {
  await loadSets();
  const queryId = Number(route.query.rule_set_id);
  const queryVersion = Number(route.query.version);
  const queryName =
    typeof route.query.rule_set_name === "string"
      ? route.query.rule_set_name
      : "";
  if (Number.isFinite(queryId) && queryId > 0) {
    selectedId.value = queryId;
    const known = sets.value.find((item) => item.id === queryId);
    if (known) selectedName.value = known.name;
    await loadVersion(queryId);
    return;
  }
  if (queryName && nameOptions.value.includes(queryName)) {
    selectedName.value = queryName;
    if (Number.isFinite(queryVersion) && queryVersion > 0) {
      const target = versionOptions.value.find(
        (item) => item.version === queryVersion,
      );
      if (target) {
        selectedId.value = target.id;
        await loadVersion(target.id);
        return;
      }
    }
    onNameChange();
  }
});
</script>

<template>
  <div class="p-4">
    <el-tabs v-model="activeTab">
      <el-tab-pane :label="canWrite ? '编辑规则' : '查看规则'" name="edit">
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <!-- 宽度挂在包裹层上：el-select 自身 width:100%，在 flex-wrap 行里会独占一行 -->
          <div class="w-56">
            <el-select
              v-model="selectedName"
              :loading="loadingSets"
              filterable
              placeholder="选择规则集"
              class="w-full"
              :disabled="!canWrite || loadingDetail"
              @change="onNameChange"
            >
              <el-option
                v-for="name in nameOptions"
                :key="name"
                :label="name"
                :value="name"
              />
            </el-select>
          </div>
          <div class="w-44">
            <el-select
              v-model="selectedId"
              placeholder="选择版本"
              class="w-full"
              :disabled="!selectedName || loadingDetail"
              @change="onVersionChange"
            >
              <el-option
                v-for="item in versionOptions"
                :key="item.id"
                :label="`v${item.version}（#${item.id}）`"
                :value="item.id"
              />
            </el-select>
          </div>
          <span class="text-xs text-gray-500">
            另存为新版本时同名版本号由后端自动 +1
          </span>
        </div>

        <el-form
          v-loading="loadingDetail"
          :model="form"
          :disabled="!canWrite"
          label-width="110px"
        >
          <el-row :gutter="12">
            <!-- 8+8+4+4=24 恰好铺满一行；此前 编码/回退编码 各占 lg=3，扣掉
                 label-width 110px 后输入框只剩约 35px，连 "utf-8" 都显示不全 -->
            <el-col :xs="24" :sm="12" :lg="8">
              <el-form-item label="规则集名称">
                <el-input v-model="form.name" clearable />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :lg="8">
              <el-form-item label="文件通配">
                <el-input v-model="form.file_glob" placeholder="**/*" />
              </el-form-item>
            </el-col>
            <el-col :xs="12" :sm="6" :lg="4">
              <el-form-item label="编码">
                <el-input v-model="form.encoding" />
              </el-form-item>
            </el-col>
            <el-col :xs="12" :sm="6" :lg="4">
              <el-form-item label="回退编码">
                <el-input v-model="form.fallback_encoding" />
              </el-form-item>
            </el-col>
          </el-row>

          <el-divider content-position="left">
            规则（{{ form.rules.length }} 条）
          </el-divider>
          <RuleTable
            :rows="form.rules"
            :readonly="!canWrite"
            @add="onAddRule"
            @remove="onRemoveRule"
            @move="onMoveRule"
          />

          <el-alert
            v-if="backendError"
            type="error"
            :closable="false"
            show-icon
            class="mt-3"
          >
            <pre class="whitespace-pre-wrap">{{ backendError }}</pre>
          </el-alert>

          <div v-if="canWrite" class="mt-3 flex items-center gap-2">
            <el-button
              type="primary"
              :loading="saving"
              :disabled="!selectedId || !form.rules.length"
              @click="onSave"
            >
              另存为新版本
            </el-button>
            <span class="text-xs text-gray-500">
              保存后留在当前编辑态，可继续调整；正则编译等校验以后端 422 为准。
            </span>
          </div>
        </el-form>
      </el-tab-pane>

      <!-- lazy：检索 Tab 的表格与筛选器较重，首次切到该页签才挂载，
           规则多时的编辑页首屏不再为其付出渲染成本 -->
      <el-tab-pane label="跨版本检索" name="search" lazy>
        <PureTableBar
          title="规则索引"
          :columns="searchColumns"
          @refresh="doSearch"
        >
          <template #buttons>
            <div class="flex flex-wrap items-center gap-2">
              <el-input
                v-model="search.keyword"
                clearable
                placeholder="规则 ID / 正则 / 关键词"
                class="w-44"
                @keyup.enter="onSearch"
              />
              <el-select
                v-model="search.type"
                clearable
                placeholder="类型"
                class="w-28"
              >
                <el-option label="keyword" value="keyword" />
                <el-option label="regex" value="regex" />
              </el-select>
              <el-input
                v-model="search.rule_set_name"
                clearable
                placeholder="规则集名称"
                class="w-36"
                @keyup.enter="onSearch"
              />
              <el-button type="primary" :loading="searching" @click="onSearch">
                检索
              </el-button>
            </div>
          </template>
          <template v-slot="{ size }">
            <pure-table
              :size="size"
              adaptive
              :loading="searching"
              :data="searchRows"
              :columns="searchColumns"
            >
              <template #type="{ row }">
                <el-tag :size="size" effect="plain">{{ row.type }}</el-tag>
              </template>
              <template #severity="{ row }">
                <el-tag :size="size" :type="severityTagType(row.severity)">
                  {{ row.severity }}
                </el-tag>
              </template>
              <template #operation="{ row }">
                <el-button
                  :size="size"
                  link
                  type="primary"
                  @click="goToEdit(row)"
                >
                  {{ canWrite ? "去编辑" : "查看" }}
                </el-button>
              </template>
            </pure-table>
            <div class="mt-3 flex items-center justify-between text-sm">
              <span class="text-gray-500">
                {{
                  searchSearched
                    ? `后端列表接口不返回总数；第 ${searchPageIndex} 页，本页 ${searchRows.length} 条`
                    : "跨版本检索是后端查询期解析 config，未执行检索"
                }}
              </span>
              <div class="flex gap-2">
                <el-button
                  :disabled="searchPage.offset === 0 || searching"
                  @click="searchPrev"
                >
                  上一页
                </el-button>
                <el-button
                  :disabled="!searchHasNext || searching"
                  @click="searchNext"
                >
                  下一页
                </el-button>
              </div>
            </div>
          </template>
        </PureTableBar>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>
