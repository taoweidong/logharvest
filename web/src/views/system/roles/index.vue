<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from "vue";
import type { FormInstance, FormRules, TreeV2Instance } from "element-plus";
import { ElMessageBox } from "element-plus";
import { useWindowSize } from "@vueuse/core";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import {
  assignRoleMenus,
  createRole,
  deleteRole,
  listRoleMenus,
  listRoles,
  menuTree,
  updateRole
} from "@/api/rbac";
import type { MenuRecord, RoleRecord } from "@/api/rbacTypes";
import { collectTreeIds } from "@/utils/tree";
import { useAppStoreHook } from "@/store/modules/app";
import {
  EMPTY_ROLE_FILTERS,
  filterRoles,
  menuTypeMeta,
  type RoleFilters
} from "../filters";
import { ADAPTIVE_OFFSET_BOTTOM, TABLE_HEADER_CELL_STYLE } from "../table";

import Refresh from "~icons/ep/refresh";
import AddFill from "~icons/ri/add-circle-line";
import EditPen from "~icons/ep/edit-pen";
import Delete from "~icons/ep/delete";
import Menu from "~icons/ep/menu";
import Close from "~icons/ep/close";
import Check from "~icons/ep/check";

defineOptions({ name: "Roles" });

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const loading = ref(false);
const rows = ref<RoleRecord[]>([]);

/** 表单值与“已应用”值分开：点搜索才生效，与上游交互一致 */
const filters = reactive<RoleFilters>({ ...EMPTY_ROLE_FILTERS });
const appliedFilters = ref<RoleFilters>({ ...EMPTY_ROLE_FILTERS });
const filteredRows = computed(() => filterRoles(rows.value, appliedFilters.value));

async function load() {
  loading.value = true;
  try {
    rows.value = await listRoles();
  } catch (error) {
    rows.value = [];
    message(apiErrorDetail(error, "角色列表加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

function onSearch() {
  appliedFilters.value = { ...filters };
}

function onResetSearch() {
  Object.assign(filters, EMPTY_ROLE_FILTERS);
  appliedFilters.value = { ...EMPTY_ROLE_FILTERS };
}

// ---------------------------------------------------------------- 新建 / 编辑

const dialogVisible = ref(false);
const editingRole = ref<RoleRecord | null>(null);
const isCreate = computed(() => editingRole.value === null);
const saving = ref(false);
const formRef = ref<FormInstance>();

const form = reactive({
  name: "",
  code: "",
  description: "",
  is_active: true
});

const rules = computed<FormRules>(() => ({
  name: [{ required: true, message: "请填写角色名称", trigger: "blur" }],
  code: isCreate.value
    ? [
        { required: true, message: "请填写角色代码", trigger: "blur" },
        {
          pattern: /^[a-z][a-z0-9_-]{1,49}$/,
          message: "以小写字母开头，仅含小写字母、数字、_ 或 -，长度 2-50",
          trigger: "blur"
        }
      ]
    : []
}));

function clearFormValidate() {
  nextTick(() => formRef.value?.clearValidate());
}

function openCreate() {
  editingRole.value = null;
  Object.assign(form, {
    name: "",
    code: "",
    description: "",
    is_active: true
  });
  dialogVisible.value = true;
  clearFormValidate();
}

function openEdit(row: RoleRecord) {
  editingRole.value = row;
  Object.assign(form, {
    name: row.name,
    code: row.code,
    description: row.description,
    is_active: row.is_active
  });
  dialogVisible.value = true;
  clearFormValidate();
}

async function onSubmit() {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
  saving.value = true;
  try {
    if (isCreate.value) {
      const created = await createRole({
        code: form.code.trim(),
        name: form.name.trim(),
        description: form.description.trim(),
        is_active: form.is_active
      });
      message(`角色“${created.name}”已创建`, { type: "success" });
    } else if (editingRole.value) {
      await updateRole(editingRole.value.id, {
        name: form.name.trim(),
        description: form.description.trim(),
        is_active: form.is_active
      });
      message("角色信息已更新", { type: "success" });
    }
    dialogVisible.value = false;
    await load();
  } catch (error) {
    message(apiErrorDetail(error, isCreate.value ? "创建角色失败" : "更新角色失败"), {
      type: "error"
    });
  } finally {
    saving.value = false;
  }
}

// ---------------------------------------------------------------- 菜单权限面板

const panelVisible = ref(false);
const panelLoading = ref(false);
const panelSaving = ref(false);
const currentRole = ref<RoleRecord | null>(null);
const treeRef = ref<TreeV2Instance>();
const rawTree = ref<MenuRecord[]>([]);
const grantedIds = ref<number[]>([]);
const treeKeyword = ref("");
const isExpandAll = ref(true);
const isSelectAll = ref(false);
const isLinkage = ref(false);

const isReadonlyRole = computed(() => !!currentRole.value?.is_system);
const grantedIdSet = computed(() => new Set(grantedIds.value));
const allMenuIds = computed(() => collectTreeIds(rawTree.value));

/** el-tree-v2 需要 value 键名；label/children 与后端菜单树字段对齐 */
const treeProps = { value: "id", label: "title", children: "children" };

const { height: windowHeight } = useWindowSize();
const panelRef = ref<HTMLElement>();
/** el-tree-v2 只接受像素高度：按面板实际位置算，避免把提示行/页脚顶出视口 */
const treeHeight = ref(360);

function recalcTreeHeight() {
  const el = panelRef.value;
  if (!el) return;
  const top = el.getBoundingClientRect().top;
  treeHeight.value = Math.max(
    240,
    Math.min(720, window.innerHeight - top - 258)
  );
}

watch([windowHeight, panelVisible], async () => {
  await nextTick();
  recalcTreeHeight();
});

function resetPanelState() {
  treeKeyword.value = "";
  isExpandAll.value = true;
  isSelectAll.value = false;
  isLinkage.value = false;
}

function applyExpanded() {
  treeRef.value?.setExpandedKeys(isExpandAll.value ? allMenuIds.value : []);
}

function applyChecked() {
  if (isReadonlyRole.value) return;
  treeRef.value?.setCheckedKeys(isSelectAll.value ? allMenuIds.value : []);
}

async function openPermissions(row: RoleRecord) {
  currentRole.value = row;
  rawTree.value = [];
  grantedIds.value = [];
  resetPanelState();
  panelVisible.value = true;
  panelLoading.value = true;
  try {
    const [tree, checkedIds] = await Promise.all([
      menuTree(),
      listRoleMenus(row.id)
    ]);
    rawTree.value = tree;
    grantedIds.value = checkedIds;
    await nextTick();
    recalcTreeHeight();
    treeRef.value?.setCheckedKeys(checkedIds);
    applyExpanded();
  } catch (error) {
    message(apiErrorDetail(error, "角色权限加载失败"), { type: "error" });
  } finally {
    panelLoading.value = false;
  }
}

function closePanel() {
  panelVisible.value = false;
  currentRole.value = null;
  rawTree.value = [];
  grantedIds.value = [];
}

function onTreeQueryChanged(query: string) {
  treeRef.value?.filter(query);
}

function filterMethod(query: string, data: MenuRecord) {
  const kw = String(query ?? "").trim().toLowerCase();
  if (!kw) return true;
  return [data.title, data.name, data.path]
    .filter(Boolean)
    .some(text => String(text).toLowerCase().includes(kw));
}

async function onSavePermissions() {
  if (!currentRole.value || isReadonlyRole.value || panelSaving.value) return;
  const checked = (treeRef.value?.getCheckedKeys() ?? []).map(Number);
  panelSaving.value = true;
  try {
    await assignRoleMenus(currentRole.value.id, checked);
    message(`角色“${currentRole.value.name}”的权限已更新`, { type: "success" });
    closePanel();
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "保存角色权限失败"), { type: "error" });
  } finally {
    panelSaving.value = false;
  }
}

// ---------------------------------------------------------------- 删除

async function onDelete(row: RoleRecord) {
  try {
    await ElMessageBox.confirm(
      `确认删除角色“${row.name}”（${row.code}）？删除后无法恢复。`,
      "删除角色",
      {
        type: "warning",
        confirmButtonText: "删除",
        cancelButtonText: "取消",
        confirmButtonClass: "el-button--danger"
      }
    );
  } catch {
    return;
  }
  try {
    await deleteRole(row.id);
    message(`角色“${row.name}”已删除`, { type: "success" });
    if (currentRole.value?.id === row.id) closePanel();
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "删除角色失败"), { type: "error" });
  }
}

// ---------------------------------------------------------------- 表格

const columns = computed<TableColumnList>(() => {
  const list: TableColumnList = [];
  // 面板打开时表格只剩 62% 宽：省掉内部 ID 列，五列才排得下、操作按钮不被裁切
  if (!panelVisible.value || isMobile.value) {
    list.push({ label: "ID", prop: "id", width: 60 });
  }
  list.push(
    {
      label: "角色名",
      prop: "name",
      slot: "name",
      minWidth: 200,
      showOverflowTooltip: true
    },
    { label: "角色标识", prop: "code", minWidth: 140, showOverflowTooltip: true },
    { label: "状态", prop: "is_active", slot: "is_active", width: 80 },
    {
      label: "操作",
      prop: "operation",
      slot: "operation",
      width: 220,
      // 面板打开时表格被压窄，固定列会盖住相邻列，改为可横向滚动
      fixed: isMobile.value || panelVisible.value ? undefined : "right"
    }
  );
  return list;
});

function rowStyle({ row }: { row: RoleRecord }) {
  return {
    background:
      row.id === currentRole.value?.id ? "var(--el-fill-color-light)" : ""
  };
}

onMounted(load);
</script>

<template>
  <div class="p-4">
    <el-form
      :inline="true"
      :model="filters"
      class="search-form bg-bg_color mb-2"
      @submit.prevent
    >
      <el-form-item label="角色名称：" prop="name">
        <el-input
          v-model="filters.name"
          placeholder="请输入角色名称"
          clearable
          class="w-45!"
          @keyup.enter="onSearch"
        />
      </el-form-item>
      <el-form-item label="角色标识：" prop="code">
        <el-input
          v-model="filters.code"
          placeholder="请输入角色标识"
          clearable
          class="w-45!"
          @keyup.enter="onSearch"
        />
      </el-form-item>
      <el-form-item label="状态：" prop="status">
        <el-select
          v-model="filters.status"
          placeholder="请选择状态"
          clearable
          class="w-45!"
        >
          <el-option label="已启用" value="active" />
          <el-option label="已停用" value="inactive" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          :icon="useRenderIcon('ri/search-line')"
          :loading="loading"
          @click="onSearch"
        >
          搜索
        </el-button>
        <el-button :icon="useRenderIcon(Refresh)" @click="onResetSearch">
          重置
        </el-button>
      </el-form-item>
    </el-form>

    <div :class="['flex', isMobile ? 'flex-wrap' : '']">
      <div
        :style="{
          width: panelVisible && !isMobile ? '62%' : '100%',
          transition: 'width 220ms cubic-bezier(0.4, 0, 0.2, 1)'
        }"
      >
        <PureTableBar title="角色管理" :columns="columns" @refresh="load">
          <template #buttons>
            <el-button
              v-perms="'roles:write'"
              type="primary"
              :icon="useRenderIcon(AddFill)"
              @click="openCreate"
            >
              新建角色
            </el-button>
          </template>
          <template v-slot="{ size, dynamicColumns }">
            <pure-table
              row-key="id"
              adaptive
              :adaptiveConfig="{ offsetBottom: ADAPTIVE_OFFSET_BOTTOM }"
              align-whole="center"
              showOverflowTooltip
              table-layout="auto"
              :loading="loading"
              :size="size"
              :data="filteredRows"
              :columns="dynamicColumns"
              :row-style="rowStyle"
              :header-cell-style="TABLE_HEADER_CELL_STYLE"
            >
              <template #name="{ row }">
                <div class="flex flex-col items-center gap-0.5 leading-5">
                  <span class="flex items-center gap-1">
                    <span>{{ row.name }}</span>
                    <el-tag
                      :size="size"
                      :type="row.is_system ? 'warning' : 'info'"
                      effect="plain"
                    >
                      {{ row.is_system ? "内置" : "自定义" }}
                    </el-tag>
                  </span>
                  <span
                    v-if="row.description"
                    class="max-w-45 truncate text-xs text-gray-400"
                    :title="row.description"
                  >
                    {{ row.description }}
                  </span>
                </div>
              </template>
              <template #is_active="{ row }">
                <el-tag
                  :size="size"
                  :type="row.is_active ? 'success' : 'info'"
                  effect="plain"
                >
                  {{ row.is_active ? "启用" : "停用" }}
                </el-tag>
              </template>
              <template #operation="{ row }">
                <el-button
                  v-perms="'roles:write'"
                  class="reset-margin"
                  link
                  type="primary"
                  :size="size"
                  :icon="useRenderIcon(EditPen)"
                  @click="openEdit(row)"
                >
                  修改
                </el-button>
                <el-button
                  v-perms="'roles:write'"
                  class="reset-margin"
                  link
                  type="primary"
                  :size="size"
                  :icon="useRenderIcon(Menu)"
                  @click="openPermissions(row)"
                >
                  权限
                </el-button>
                <el-button
                  v-if="!row.is_system"
                  v-perms="'roles:write'"
                  class="reset-margin"
                  link
                  type="danger"
                  :size="size"
                  :icon="useRenderIcon(Delete)"
                  @click="onDelete(row)"
                >
                  删除
                </el-button>
              </template>
            </pure-table>
            <div class="mt-3 flex items-center justify-between text-sm">
              <span class="text-gray-500">
                内置角色由系统维护：不可删除，角色代码、启用状态与菜单权限均只读。
              </span>
              <span class="text-gray-500">共 {{ filteredRows.length }} 条</span>
            </div>
          </template>
        </PureTableBar>
      </div>

      <div
        v-if="panelVisible"
        ref="panelRef"
        v-loading="panelLoading"
        class="bg-bg_color px-2 pb-2"
        :class="isMobile ? 'mt-2 w-full' : 'ml-2 min-w-0 flex-1'"
      >
        <div class="flex w-full items-center justify-between px-3 pt-5 pb-4">
          <div class="flex items-center">
            <span
              class="panel-icon"
              role="button"
              aria-label="关闭菜单权限面板"
              v-tippy="{ content: '关闭' }"
              @click="closePanel"
            >
              <IconifyIconOffline :icon="Close" width="18" height="18" />
            </span>
            <span
              v-if="!isReadonlyRole"
              class="panel-icon ml-2"
              :class="panelSaving ? 'pointer-events-none opacity-50' : ''"
              role="button"
              aria-label="保存菜单权限"
              v-tippy="{ content: '保存菜单权限' }"
              @click="onSavePermissions"
            >
              <IconifyIconOffline :icon="Check" width="18" height="18" />
            </span>
          </div>
          <p class="truncate font-bold">
            菜单权限{{ currentRole ? `（${currentRole.name}）` : "" }}
          </p>
        </div>

        <el-alert
          v-if="isReadonlyRole"
          type="info"
          :closable="false"
          show-icon
          title="内置角色的菜单权限由系统维护，此处只读查看。"
          class="mb-2"
        />

        <el-input
          v-model="treeKeyword"
          placeholder="请输入菜单名称、权限码或路径进行搜索"
          class="mb-1"
          clearable
          @input="onTreeQueryChanged"
        />

        <div class="flex flex-wrap">
          <el-checkbox v-model="isExpandAll" @change="applyExpanded">
            展开/折叠
          </el-checkbox>
          <template v-if="!isReadonlyRole">
            <el-checkbox v-model="isSelectAll" @change="applyChecked">
              全选/全不选
            </el-checkbox>
            <el-checkbox v-model="isLinkage" v-tippy="{ content: '开启后勾选父级会联动勾选子级；后端按精确授权存储' }">
              父子联动
            </el-checkbox>
          </template>
        </div>

        <el-tree-v2
          ref="treeRef"
          :data="rawTree"
          :props="treeProps"
          :height="treeHeight"
          :show-checkbox="!isReadonlyRole"
          :check-strictly="!isLinkage"
          :filter-method="filterMethod"
          :expand-on-click-node="false"
        >
          <template #default="{ node }">
            <span class="flex items-center gap-2">
              <span>{{ node.data.title }}</span>
              <el-tag
                size="small"
                :type="menuTypeMeta(node.data.menu_type).tag"
                effect="plain"
              >
                {{ menuTypeMeta(node.data.menu_type).label }}
              </el-tag>
              <el-tag v-if="!node.data.is_active" size="small" type="info" effect="plain">
                停用
              </el-tag>
              <el-tag
                v-if="isReadonlyRole && grantedIdSet.has(node.data.id)"
                size="small"
                type="success"
                effect="plain"
              >
                已授权
              </el-tag>
              <span class="text-xs text-gray-400">{{ node.data.name || "—" }}</span>
            </span>
          </template>
        </el-tree-v2>

        <p v-if="!isReadonlyRole" class="mt-2 text-xs text-gray-500">
          精确授权：默认不联动，勾选父级不会自动勾选子级；目录、页面与权限码需分别勾选。
        </p>
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="isCreate ? '新建角色' : '编辑角色'"
      width="min(560px, 92vw)"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="角色名" prop="name">
          <el-input v-model="form.name" clearable placeholder="面向管理员的显示名称" />
        </el-form-item>
        <el-form-item label="角色代码" prop="code">
          <el-input
            v-model="form.code"
            clearable
            :disabled="!isCreate"
            placeholder="如 ops_admin，创建后不可修改"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch
            v-model="form.is_active"
            :disabled="!isCreate && !!editingRole?.is_system"
          />
          <span
            v-if="!isCreate && editingRole?.is_system"
            class="ml-2 text-xs text-gray-500"
          >
            内置角色的启用状态不可修改
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSubmit">
          {{ isCreate ? "创建" : "保存" }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style lang="scss" scoped>
.panel-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  cursor: pointer;
  border-radius: 4px;
  outline: none;
  transition: background-color var(--pure-duration-fast, 0.15s)
    var(--pure-ease-standard, ease);

  &:hover {
    background-color: var(--el-fill-color);
  }
}
</style>
