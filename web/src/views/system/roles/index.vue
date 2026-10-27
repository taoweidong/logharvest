<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import type { FormInstance, FormRules, TreeInstance } from "element-plus";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
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
import type { MenuRecord, MenuType, RoleRecord } from "@/api/rbacTypes";
import { useAppStoreHook } from "@/store/modules/app";

defineOptions({ name: "Roles" });

type TagType = "primary" | "success" | "warning" | "info" | "danger";

const MENU_TYPE_META: Record<MenuType, { label: string; tag: TagType }> = {
  0: { label: "目录", tag: "info" },
  1: { label: "页面", tag: "primary" },
  2: { label: "权限", tag: "warning" }
};

function menuTypeMeta(type: MenuType | number) {
  return MENU_TYPE_META[type as MenuType] ?? { label: "未知", tag: "info" as TagType };
}

const loading = ref(false);
const rows = ref<RoleRecord[]>([]);

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

// ---------------------------------------------------------------- 分配权限

const permVisible = ref(false);
const permLoading = ref(false);
const permSaving = ref(false);
const permRole = ref<RoleRecord | null>(null);
const treeRef = ref<TreeInstance>();
const treeData = ref<MenuRecord[]>([]);
const pendingCheckedIds = ref<number[]>([]);

const permTitle = computed(() =>
  permRole.value ? `分配权限 - ${permRole.value.name}` : "分配权限"
);

const treeProps = { label: "title", children: "children" };

function collectMenuIds(nodes: MenuRecord[]): number[] {
  return nodes.flatMap(node => [
    node.id,
    ...collectMenuIds(node.children ?? [])
  ]);
}

const allMenuIds = computed(() => collectMenuIds(treeData.value));

function applyCheckedKeys() {
  treeRef.value?.setCheckedKeys(pendingCheckedIds.value);
}

function onPermissionsOpened() {
  // 弹窗动画结束后再回填，确保 el-tree 已完成渲染
  applyCheckedKeys();
}

async function openPermissions(row: RoleRecord) {
  permRole.value = row;
  treeData.value = [];
  pendingCheckedIds.value = [];
  permVisible.value = true;
  permLoading.value = true;
  try {
    const [tree, checkedIds] = await Promise.all([
      menuTree(),
      listRoleMenus(row.id)
    ]);
    treeData.value = tree;
    pendingCheckedIds.value = checkedIds;
    await nextTick();
    applyCheckedKeys();
  } catch (error) {
    message(apiErrorDetail(error, "角色权限加载失败"), { type: "error" });
  } finally {
    permLoading.value = false;
  }
}

function checkAllMenus() {
  treeRef.value?.setCheckedKeys(allMenuIds.value);
}

function clearAllMenus() {
  treeRef.value?.setCheckedKeys([]);
}

async function onSavePermissions() {
  if (!permRole.value || permRole.value.is_system) return;
  const checked = treeRef.value?.getCheckedKeys() ?? [];
  permSaving.value = true;
  try {
    await assignRoleMenus(permRole.value.id, checked.map(Number));
    message(`角色“${permRole.value.name}”的权限已更新`, { type: "success" });
    permVisible.value = false;
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "保存角色权限失败"), { type: "error" });
  } finally {
    permSaving.value = false;
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
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "删除角色失败"), { type: "error" });
  }
}

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const columns = computed<TableColumnList>(() => [
  { label: "ID", prop: "id", width: 70 },
  { label: "角色名", prop: "name", minWidth: 150, showOverflowTooltip: true },
  { label: "code", prop: "code", minWidth: 150, showOverflowTooltip: true },
  { label: "描述", prop: "description", minWidth: 220, showOverflowTooltip: true },
  { label: "类型", prop: "is_system", slot: "is_system", width: 80 },
  { label: "状态", prop: "is_active", slot: "is_active", width: 80 },
  {
    label: "操作",
    prop: "operation",
    slot: "operation",
    width: 220,
    fixed: isMobile.value ? undefined : "right"
  }
]);

onMounted(load);
</script>

<template>
  <div class="p-4">
    <PureTableBar title="角色管理" :columns="columns" @refresh="load">
      <template #buttons>
        <div class="flex items-center gap-2">
          <el-button :disabled="loading" @click="load">刷新</el-button>
          <el-button v-perms="'roles:write'" type="primary" @click="openCreate">
            新建角色
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
          <template #is_system="{ row }">
            <el-tag
              :size="size"
              :type="row.is_system ? 'warning' : 'info'"
              effect="plain"
            >
              {{ row.is_system ? "内置" : "自定义" }}
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
              v-perms="'roles:write'"
              :size="size"
              link
              type="primary"
              @click="openEdit(row)"
            >
              编辑
            </el-button>
            <el-button
              v-perms="'roles:write'"
              :size="size"
              link
              type="primary"
              @click="openPermissions(row)"
            >
              分配权限
            </el-button>
            <el-button
              v-if="!row.is_system"
              v-perms="'roles:write'"
              :size="size"
              link
              type="danger"
              @click="onDelete(row)"
            >
              删除
            </el-button>
          </template>
        </pure-table>
        <p class="mt-3 text-sm text-gray-500">
          内置角色由系统维护：不可删除，角色代码与启用状态不可修改。
        </p>
      </template>
    </PureTableBar>

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

    <el-dialog
      v-model="permVisible"
      :title="permTitle"
      width="min(680px, 94vw)"
      :close-on-click-modal="false"
      append-to-body
      @opened="onPermissionsOpened"
    >
      <div v-loading="permLoading">
        <el-alert
          v-if="permRole?.is_system"
          type="info"
          :closable="false"
          show-icon
          title="内置角色由系统维护，权限只读查看，不能在此修改。"
          class="mb-3"
        />
        <div class="mb-2 flex items-center justify-between gap-3">
          <span class="text-xs text-gray-500">
            精确授权：勾选父节点不会自动勾选子节点；目录、页面与权限码需分别勾选。
          </span>
          <div v-if="!permRole?.is_system" class="flex gap-2">
            <el-button size="small" @click="checkAllMenus">全选</el-button>
            <el-button size="small" @click="clearAllMenus">清空</el-button>
          </div>
        </div>
        <el-tree
          ref="treeRef"
          :data="treeData"
          node-key="id"
          :props="treeProps"
          :show-checkbox="!permRole?.is_system"
          check-strictly
          default-expand-all
          :expand-on-click-node="false"
          class="max-h-[55vh] overflow-auto"
        >
          <template #default="{ data }">
            <span class="flex items-center gap-2">
              <span>{{ data.title }}</span>
              <el-tag
                size="small"
                :type="menuTypeMeta(data.menu_type).tag"
                effect="plain"
              >
                {{ menuTypeMeta(data.menu_type).label }}
              </el-tag>
              <el-tag v-if="!data.is_active" size="small" type="info" effect="plain">
                停用
              </el-tag>
              <span class="text-xs text-gray-400">{{ data.name || "—" }}</span>
            </span>
          </template>
        </el-tree>
      </div>
      <template #footer>
        <el-button @click="permVisible = false">关闭</el-button>
        <el-button
          v-if="!permRole?.is_system"
          type="primary"
          :loading="permSaving"
          :disabled="permLoading"
          @click="onSavePermissions"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
