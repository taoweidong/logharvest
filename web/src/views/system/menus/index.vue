<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import type { FormInstance, FormRules } from "element-plus";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import { createMenu, deleteMenu, menuTree, updateMenu } from "@/api/rbac";
import type { MenuPayload, MenuRecord, MenuType } from "@/api/rbacTypes";
import { useAppStoreHook } from "@/store/modules/app";
import { filterMenus, menuTypeMeta } from "../filters";
import { TABLE_HEADER_CELL_STYLE } from "@/config/table";

import Refresh from "~icons/ep/refresh";
import AddFill from "~icons/ri/add-circle-line";
import EditPen from "~icons/ep/edit-pen";
import Delete from "~icons/ep/delete";

defineOptions({ name: "Menus" });

const ROOT_VALUE = 0;

const loading = ref(false);
const tree = ref<MenuRecord[]>([]);
const tableRef = ref();

/** 菜单树一次取全量，关键字筛选在客户端对整棵树做（保留命中节点的祖先链） */
const filters = reactive({ keyword: "" });
const appliedKeyword = ref("");
const filteredTree = computed(() => filterMenus(tree.value, appliedKeyword.value));

function onSearch() {
  appliedKeyword.value = filters.keyword;
}

function onResetSearch() {
  filters.keyword = "";
  appliedKeyword.value = "";
}

async function load() {
  loading.value = true;
  try {
    tree.value = await menuTree();
  } catch (error) {
    tree.value = [];
    message(apiErrorDetail(error, "菜单树加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------- 上级选项

function walkMenus(
  nodes: MenuRecord[],
  depth: number,
  visit: (node: MenuRecord, depth: number) => void
) {
  for (const node of nodes) {
    visit(node, depth);
    if (node.children?.length) walkMenus(node.children, depth + 1, visit);
  }
}

function findMenu(nodes: MenuRecord[], id: number | null): MenuRecord | null {
  if (id === null) return null;
  for (const node of nodes) {
    if (node.id === id) return node;
    const found = findMenu(node.children ?? [], id);
    if (found) return found;
  }
  return null;
}

function collectSubtreeIds(node: MenuRecord | null): Set<number> {
  const ids = new Set<number>();
  if (!node) return ids;
  const visit = (current: MenuRecord) => {
    ids.add(current.id);
    (current.children ?? []).forEach(visit);
  };
  visit(node);
  return ids;
}

const editingId = ref<number | null>(null);
const editingSystem = ref(false);
const isEdit = computed(() => editingId.value !== null);

const excludedIds = computed(() =>
  collectSubtreeIds(findMenu(tree.value, editingId.value))
);

/** 父节点合法性：目录挂在根或目录下；页面挂在目录下；权限挂在页面下。 */
const parentOptions = computed(() => {
  const options: Array<{ value: number; label: string }> = [];
  // 只有目录能挂在根下：页面挂根会被后端拒绝（root menu must be a directory），
  // 把「根目录」放进页面/权限的可选项等于给出一个必然报错的选择。
  if (form.menu_type === 0) {
    options.push({ value: ROOT_VALUE, label: "根目录" });
  }
  walkMenus(tree.value, 0, (node, depth) => {
    if (excludedIds.value.has(node.id)) return;
    const wanted = form.menu_type === 2 ? node.menu_type === 1 : node.menu_type === 0;
    if (!wanted) return;
    options.push({
      value: node.id,
      label: `${"　".repeat(depth)}${node.title || node.name}（${
        menuTypeMeta(node.menu_type).label
      }）`
    });
  });
  return options;
});

// ---------------------------------------------------------------- 新建 / 编辑

const dialogVisible = ref(false);
const mode = ref<"create" | "edit">("create");
const saving = ref(false);
const formRef = ref<FormInstance>();

const form = reactive({
  parent_id: ROOT_VALUE,
  name: "",
  menu_type: 1 as MenuType,
  path: "",
  component: "",
  title: "",
  icon: "",
  rank: 0,
  show_link: true,
  keep_alive: false,
  is_active: true
});

const isTypeLocked = computed(() => mode.value === "edit");

const rules = computed<FormRules>(() => ({
  title: [{ required: true, message: "请填写标题", trigger: "blur" }],
  name: [
    { required: true, message: "请填写名称/权限码", trigger: "blur" },
    ...(form.menu_type === 2
      ? [
          {
            pattern: /^[a-z][a-z0-9:_-]{1,63}$/,
            message: "权限码需以小写字母开头，仅含小写字母、数字、: _ -",
            trigger: "blur"
          }
        ]
      : [])
  ],
  parent_id:
    form.menu_type === 0
      ? []
      : [
          {
            validator: (_rule, value, callback) => {
              if (!value) callback(new Error("页面/权限必须选择上级节点"));
              else callback();
            },
            trigger: "change"
          }
        ],
  path:
    form.menu_type === 2
      ? []
      : [{ required: true, message: "请填写路径", trigger: "blur" }],
  component:
    form.menu_type === 1
      ? [{ required: true, message: "请填写组件路径", trigger: "blur" }]
      : []
}));

function clearFormValidate() {
  nextTick(() => formRef.value?.clearValidate());
}

function resetForm() {
  Object.assign(form, {
    parent_id: ROOT_VALUE,
    name: "",
    menu_type: 1 as MenuType,
    path: "",
    component: "",
    title: "",
    icon: "",
    rank: 0,
    show_link: true,
    keep_alive: false,
    is_active: true
  });
}

function openCreate() {
  mode.value = "create";
  editingId.value = null;
  editingSystem.value = false;
  resetForm();
  dialogVisible.value = true;
  clearFormValidate();
}

function openEdit(row: any) {
  mode.value = "edit";
  editingId.value = row.id;
  editingSystem.value = row.is_system;
  Object.assign(form, {
    parent_id: row.parent_id ?? ROOT_VALUE,
    name: row.name,
    menu_type: row.menu_type,
    path: row.path,
    component: row.component,
    title: row.title,
    icon: row.icon,
    rank: row.rank,
    show_link: row.show_link,
    keep_alive: row.keep_alive,
    is_active: row.is_active
  });
  dialogVisible.value = true;
  clearFormValidate();
}

function onTypeChange() {
  form.parent_id = ROOT_VALUE;
  form.path = "";
  form.component = "";
  form.icon = "";
  form.keep_alive = false;
  form.show_link = form.menu_type !== 2;
  clearFormValidate();
}

function buildPayload(): MenuPayload {
  return {
    parent_id: form.parent_id === ROOT_VALUE ? null : form.parent_id,
    name: form.name.trim(),
    menu_type: form.menu_type,
    path: form.path.trim(),
    component: form.menu_type === 1 ? form.component.trim() : "",
    title: form.title.trim(),
    icon: form.menu_type === 2 ? "" : form.icon.trim(),
    rank: Number(form.rank) || 0,
    show_link: form.menu_type === 2 ? false : form.show_link,
    keep_alive: form.menu_type === 1 ? form.keep_alive : false,
    is_active: form.is_active
  };
}

async function onSubmit() {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;

  saving.value = true;
  try {
    if (mode.value === "create") {
      const created = await createMenu(buildPayload());
      message(`菜单“${created.title || created.name}”已创建`, {
        type: "success"
      });
    } else if (editingId.value !== null) {
      const updated = await updateMenu(editingId.value, buildPayload());
      message(`菜单“${updated.title || updated.name}”已更新`, {
        type: "success"
      });
    }
    dialogVisible.value = false;
    await load();
  } catch (error) {
    message(
      apiErrorDetail(error, mode.value === "create" ? "创建菜单失败" : "更新菜单失败"),
      { type: "error" }
    );
  } finally {
    saving.value = false;
  }
}

// ---------------------------------------------------------------- 删除

async function onDelete(row: any) {
  try {
    await ElMessageBox.confirm(
      `确认删除菜单“${row.title || row.name}”？其权限授权会被一并清理，且无法恢复。`,
      "删除菜单",
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
    await deleteMenu(row.id);
    message(`菜单“${row.title || row.name}”已删除`, { type: "success" });
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "删除菜单失败"), { type: "error" });
  }
}

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const columns = computed<TableColumnList>(() => [
  { label: "标题", prop: "title", minWidth: 180, showOverflowTooltip: true },
  { label: "类型", prop: "menu_type", slot: "menu_type", width: 90 },
  { label: "名称/权限码", prop: "name", minWidth: 140, showOverflowTooltip: true },
  { label: "路径", prop: "path", minWidth: 160, showOverflowTooltip: true },
  { label: "组件", prop: "component", slot: "component", minWidth: 170 },
  { label: "排序", prop: "rank", width: 70 },
  { label: "显示", prop: "show_link", slot: "show_link", width: 80 },
  { label: "状态", prop: "is_active", slot: "is_active", width: 80 },
  {
    label: "操作",
    prop: "operation",
    slot: "operation",
    width: 170,
    fixed: isMobile.value ? undefined : "right"
  }
]);

onMounted(load);
</script>

<template>
  <div class="page">
    <section class="page-panel">
      <el-form
        :inline="true"
        :model="filters"
        class="search-form"
        @submit.prevent
      >
        <el-form-item label="菜单名称：" prop="keyword">
          <el-input
            v-model="filters.keyword"
            placeholder="搜索标题 / 权限码 / 路径"
            clearable
            class="w-45!"
            @keyup.enter="onSearch"
            @clear="onResetSearch"
          />
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
    </section>

    <PureTableBar
      title="菜单管理"
      :columns="columns"
      :table-ref="tableRef"
      @refresh="load"
    >
      <template #buttons>
        <el-button
          v-perms="'menus:write'"
          type="primary"
          :icon="useRenderIcon(AddFill)"
          @click="openCreate"
        >
          新建菜单
        </el-button>
      </template>
      <template v-slot="{ size, dynamicColumns }">
        <pure-table
          ref="tableRef"
          row-key="id"
          height="100%"
          align-whole="center"
          table-layout="auto"
          default-expand-all
          :tree-props="{
            hasChildren: 'hasChildren',
            children: 'children',
            checkStrictly: false
          }"
          :loading="loading"
          :size="size"
          :data="filteredTree"
          :columns="dynamicColumns"
          :header-cell-style="TABLE_HEADER_CELL_STYLE"
        >
          <template #menu_type="{ row }">
            <el-tag
              :size="size"
              :type="menuTypeMeta(row.menu_type).tag"
              effect="plain"
            >
              {{ menuTypeMeta(row.menu_type).label }}
            </el-tag>
          </template>
          <template #component="{ row }">
            {{ row.component || "—" }}
          </template>
          <template #show_link="{ row }">
            <el-tag
              :size="size"
              :type="row.show_link ? 'success' : 'info'"
              effect="plain"
            >
              {{ row.show_link ? "是" : "否" }}
            </el-tag>
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
              v-perms="'menus:write'"
              class="reset-margin"
              :size="size"
              link
              type="primary"
              :icon="useRenderIcon(EditPen)"
              @click="openEdit(row)"
            >
              修改
            </el-button>
            <el-button
              v-if="!row.is_system"
              v-perms="'menus:write'"
              class="reset-margin"
              :size="size"
              link
              type="danger"
              :icon="useRenderIcon(Delete)"
              @click="onDelete(row)"
            >
              删除
            </el-button>
            <el-tag v-else :size="size" type="warning" effect="plain">
              内置
            </el-tag>
          </template>
        </pure-table>
      </template>
      <template #footer>
        <span>
          内置菜单由系统维护，不能删除；名称、类型、上级、路径、组件不可修改，仅可调整标题、图标、排序与显示状态。
        </span>
      </template>
    </PureTableBar>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑菜单' : '新建菜单'"
      width="min(620px, 94vw)"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-alert
        v-if="editingSystem"
        type="info"
        :closable="false"
        show-icon
        title="这是内置菜单：名称、类型、上级、路径与组件由系统固定，只能修改展示类字段。"
        class="mb-3"
      />
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="类型" prop="menu_type">
          <el-radio-group
            v-model="form.menu_type"
            :disabled="isTypeLocked"
            @change="onTypeChange"
          >
            <el-radio :value="0">目录</el-radio>
            <el-radio :value="1">页面</el-radio>
            <el-radio :value="2">权限</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="上级" prop="parent_id">
          <el-select
            v-model="form.parent_id"
            filterable
            class="w-full"
            :disabled="editingSystem"
            :placeholder="
              form.menu_type === 2
                ? '选择所属页面'
                : form.menu_type === 1
                  ? '选择所属目录'
                  : '选择上级目录'
            "
          >
            <el-option
              v-for="option in parentOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标题" prop="title">
          <el-input v-model="form.title" clearable placeholder="显示名称" />
        </el-form-item>
        <el-form-item label="名称/权限码" prop="name">
          <el-input
            v-model="form.name"
            clearable
            :disabled="editingSystem"
            :placeholder="
              form.menu_type === 2 ? '如 jobs:read' : '路由 name，如 Jobs'
            "
          />
        </el-form-item>
        <el-form-item
          v-if="form.menu_type !== 2"
          label="路径"
          prop="path"
        >
          <el-input
            v-model="form.path"
            clearable
            :disabled="editingSystem"
            placeholder="如 /logharvest/jobs"
          />
        </el-form-item>
        <el-form-item
          v-if="form.menu_type === 1"
          label="组件"
          prop="component"
        >
          <el-input
            v-model="form.component"
            clearable
            :disabled="editingSystem"
            placeholder="如 logharvest/jobs/index"
          />
        </el-form-item>
        <el-form-item v-if="form.menu_type !== 2" label="图标">
          <el-input v-model="form.icon" clearable placeholder="如 ep/document" />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="form.rank" :min="0" :max="9999" />
        </el-form-item>
        <el-form-item v-if="form.menu_type !== 2" label="显示在菜单">
          <el-switch v-model="form.show_link" />
        </el-form-item>
        <el-form-item v-if="form.menu_type === 1" label="页面缓存">
          <el-switch v-model="form.keep_alive" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSubmit">
          {{ isEdit ? "保存" : "创建" }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
