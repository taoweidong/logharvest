<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import type { FormInstance, FormRules } from "element-plus";
import { ElMessageBox } from "element-plus";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { message } from "@/utils/message";
import { apiErrorDetail } from "@/api/logharvest";
import {
  assignUserRoles,
  createUser,
  deleteUser,
  listRoles,
  listUsers,
  resetUserPassword,
  updateUser
} from "@/api/rbac";
import type { RoleRecord, UserRecord } from "@/api/rbacTypes";
import { formatDateTime } from "@/views/logharvest/format";
import { useAppStoreHook } from "@/store/modules/app";
import { ADAPTIVE_OFFSET_BOTTOM, TABLE_HEADER_CELL_STYLE } from "../table";

import Refresh from "~icons/ep/refresh";
import AddFill from "~icons/ri/add-circle-line";
import EditPen from "~icons/ep/edit-pen";
import Delete from "~icons/ep/delete";
import Key from "~icons/ri/key-2-line";
import Role from "~icons/ri/admin-line";
import More from "~icons/ep/more-filled";

defineOptions({ name: "Users" });

const loading = ref(false);
const rows = ref<UserRecord[]>([]);
const roles = ref<RoleRecord[]>([]);

/** 关键字下推给后端（`?keyword=`），列表接口本身不支持状态筛选 */
const filters = reactive({ keyword: "" });
/** 后端列表接口是裸数组且没有 total，沿用窗口式 limit/offset 翻页 */
const page = reactive({ limit: 20, offset: 0 });const hasNext = computed(() => rows.value.length === page.limit);
const pageIndex = computed(() => Math.floor(page.offset / page.limit) + 1);

async function load() {
  loading.value = true;
  try {
    rows.value = await listUsers({
      limit: page.limit,
      offset: page.offset,
      keyword: filters.keyword.trim() || undefined
    });
  } catch (error) {
    rows.value = [];
    message(apiErrorDetail(error, "用户列表加载失败"), { type: "error" });
  } finally {
    loading.value = false;
  }
}

async function loadRoles() {
  try {
    roles.value = await listRoles();
  } catch (error) {
    roles.value = [];
    message(apiErrorDetail(error, "角色列表加载失败"), { type: "error" });
  }
}

function onSearch() {
  page.offset = 0;
  load();
}

function onResetSearch() {
  filters.keyword = "";
  page.offset = 0;
  load();
}

function goPrev() {
  page.offset = Math.max(0, page.offset - page.limit);
  load();
}

function goNext() {
  page.offset += page.limit;
  load();
}

// ---------------------------------------------------------------- 新建 / 编辑

const dialogVisible = ref(false);
const mode = ref<"create" | "edit">("create");
const editingId = ref<number | null>(null);
const isCreate = computed(() => mode.value === "create");
const saving = ref(false);
const formRef = ref<FormInstance>();

const form = reactive({
  username: "",
  nickname: "",
  password: "",
  is_active: true,
  role_ids: [] as number[]
});

const rules = computed<FormRules>(() => ({
  username: [
    { required: true, message: "请填写用户名", trigger: "blur" },
    {
      // 与后端 UserCreate 的同名约束一致，避免把 422 当成接口错误弹出来。
      pattern: /^[A-Za-z0-9][A-Za-z0-9_.@-]{1,49}$/,
      message: "以字母或数字开头，仅含字母、数字、. _ @ -，长度 2-50",
      trigger: "blur"
    }
  ],
  password: isCreate.value
    ? [
        { required: true, message: "请填写初始密码", trigger: "blur" },
        { min: 8, message: "密码至少 8 位", trigger: "blur" }
      ]
    : []
}));

function clearFormValidate() {
  nextTick(() => formRef.value?.clearValidate());
}

function openCreate() {
  mode.value = "create";
  editingId.value = null;
  Object.assign(form, {
    username: "",
    nickname: "",
    password: "",
    is_active: true,
    role_ids: []
  });
  dialogVisible.value = true;
  clearFormValidate();
}

function openEdit(row: UserRecord) {
  mode.value = "edit";
  editingId.value = row.id;
  Object.assign(form, {
    username: row.username,
    nickname: row.nickname,
    password: "",
    is_active: row.is_active,
    role_ids: []
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
      const username = form.username.trim();
      await createUser({
        username,
        password: form.password,
        nickname: form.nickname.trim() || undefined,
        is_active: form.is_active,
        role_ids: form.role_ids
      });
      message(`用户“${username}”已创建`, { type: "success" });
    } else if (editingId.value !== null) {
      await updateUser(editingId.value, {
        nickname: form.nickname.trim(),
        is_active: form.is_active
      });
      message("用户信息已更新", { type: "success" });
    }
    dialogVisible.value = false;
    await load();
  } catch (error) {
    message(apiErrorDetail(error, isCreate.value ? "创建用户失败" : "更新用户失败"), {
      type: "error"
    });
  } finally {
    saving.value = false;
  }
}

// ---------------------------------------------------------------- 分配角色

const assignVisible = ref(false);
const assignTarget = ref<UserRecord | null>(null);
const assignRoleIds = ref<number[]>([]);
const assigning = ref(false);

function openAssign(row: UserRecord) {
  assignTarget.value = row;
  assignRoleIds.value = row.roles.map(role => role.id);
  assignVisible.value = true;
}

async function onAssign() {
  if (!assignTarget.value) return;
  assigning.value = true;
  try {
    await assignUserRoles(assignTarget.value.id, assignRoleIds.value);
    message(`用户“${assignTarget.value.username}”的角色已更新`, {
      type: "success"
    });
    assignVisible.value = false;
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "分配角色失败"), { type: "error" });
  } finally {
    assigning.value = false;
  }
}

// ---------------------------------------------------------------- 重置密码

const resetVisible = ref(false);
const resetTarget = ref<UserRecord | null>(null);
const resetFormRef = ref<FormInstance>();
const resetSaving = ref(false);
const resetForm = reactive({ password: "", confirm: "" });

const resetRules = computed<FormRules>(() => ({
  password: [
    { required: true, message: "请填写新密码", trigger: "blur" },
    { min: 8, message: "密码至少 8 位", trigger: "blur" }
  ],
  confirm: [
    { required: true, message: "请再次输入新密码", trigger: "blur" },
    {
      validator: (_rule, value, callback) => {
        if (value !== resetForm.password) {
          callback(new Error("两次输入的密码不一致"));
        } else {
          callback();
        }
      },
      trigger: "blur"
    }
  ]
}));

function openReset(row: UserRecord) {
  resetTarget.value = row;
  resetForm.password = "";
  resetForm.confirm = "";
  resetVisible.value = true;
  nextTick(() => resetFormRef.value?.clearValidate());
}

async function onResetSubmit() {
  if (!resetFormRef.value || !resetTarget.value) return;
  const valid = await resetFormRef.value.validate().catch(() => false);
  if (!valid) return;
  resetSaving.value = true;
  try {
    await resetUserPassword(resetTarget.value.id, resetForm.password);
    message(`用户“${resetTarget.value.username}”的密码已重置`, {
      type: "success"
    });
    resetVisible.value = false;
  } catch (error) {
    message(apiErrorDetail(error, "重置密码失败"), { type: "error" });
  } finally {
    resetSaving.value = false;
  }
}

// ---------------------------------------------------------------- 删除

async function onDelete(row: UserRecord) {
  try {
    await ElMessageBox.confirm(
      `确认删除用户“${row.username}”？删除后无法恢复。`,
      "删除用户",
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
    await deleteUser(row.id);
    message(`用户“${row.username}”已删除`, { type: "success" });
    // 当前页删空时回退一页，避免停在空列表
    if (rows.value.length === 1 && page.offset > 0) {
      page.offset -= page.limit;
    }
    await load();
  } catch (error) {
    message(apiErrorDetail(error, "删除用户失败"), { type: "error" });
  }
}

const appStore = useAppStoreHook();
const isMobile = computed(() => appStore.device === "mobile");

const columns = computed<TableColumnList>(() => [
  { label: "ID", prop: "id", width: 70 },
  { label: "用户名", prop: "username", minWidth: 130, showOverflowTooltip: true },
  { label: "昵称", prop: "nickname", minWidth: 130, showOverflowTooltip: true },
  { label: "角色", prop: "roles", slot: "roles", minWidth: 150 },
  { label: "状态", prop: "is_active", slot: "is_active", width: 80 },
  { label: "创建时间", prop: "created_at", slot: "created_at", width: 160 },
  {
    label: "操作",
    prop: "operation",
    slot: "operation",
    width: 250,
    fixed: isMobile.value ? undefined : "right"
  }
]);

onMounted(() => {
  load();
  loadRoles();
});
</script>

<template>
  <div class="p-4">
    <el-form
      :inline="true"
      :model="filters"
      class="search-form bg-bg_color mb-2"
      @submit.prevent
    >
      <el-form-item label="用户名称：" prop="keyword">
        <el-input
          v-model="filters.keyword"
          placeholder="搜索用户名 / 昵称"
          clearable
          class="w-45!"
          @keyup.enter="onSearch"
          @clear="onSearch"
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

    <PureTableBar title="用户管理" :columns="columns" @refresh="load">
      <template #buttons>
        <el-button
          v-perms="'users:write'"
          type="primary"
          :icon="useRenderIcon(AddFill)"
          @click="openCreate"
        >
          新建用户
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
          :data="rows"
          :columns="dynamicColumns"
          :header-cell-style="TABLE_HEADER_CELL_STYLE"
        >
          <template #roles="{ row }">
            <div v-if="row.roles.length" class="flex flex-wrap gap-1">
              <el-tag
                v-for="role in row.roles"
                :key="role.id"
                :size="size"
                effect="plain"
              >
                {{ role.name }}
              </el-tag>
            </div>
            <span v-else class="text-gray-400">未分配</span>
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
          <template #created_at="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
          <template #operation="{ row }">
            <el-button
              v-perms="'users:write'"
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
              v-perms="'users:write'"
              class="reset-margin"
              :size="size"
              link
              type="primary"
              :icon="useRenderIcon(Role)"
              @click="openAssign(row)"
            >
              分配角色
            </el-button>
            <el-dropdown trigger="click">
              <el-button
                class="ml-2! mt-0.5!"
                link
                type="primary"
                :size="size"
                :icon="useRenderIcon(More)"
              />
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item>
                    <el-button
                      v-perms="'users:write'"
                      class="reset-margin"
                      :size="size"
                      link
                      type="primary"
                      :icon="useRenderIcon(Key)"
                      @click="openReset(row)"
                    >
                      重置密码
                    </el-button>
                  </el-dropdown-item>
                  <el-dropdown-item>
                    <el-button
                      v-perms="'users:write'"
                      class="reset-margin"
                      :size="size"
                      link
                      type="danger"
                      :icon="useRenderIcon(Delete)"
                      @click="onDelete(row)"
                    >
                      删除
                    </el-button>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </pure-table>
        <div class="mt-3 flex items-center justify-between text-sm">
          <span class="text-gray-500">
            后端列表接口不返回总数；第 {{ pageIndex }} 页，本页 {{ rows.length }} 条
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

    <el-dialog
      v-model="dialogVisible"
      :title="isCreate ? '新建用户' : '编辑用户'"
      width="min(560px, 92vw)"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            clearable
            :disabled="!isCreate"
            placeholder="登录名，唯一"
          />
        </el-form-item>
        <el-form-item label="昵称">
          <el-input v-model="form.nickname" clearable placeholder="显示名称（可选）" />
        </el-form-item>
        <el-form-item v-if="isCreate" label="初始密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="至少 8 位"
          />
        </el-form-item>
        <el-form-item v-if="isCreate" label="角色">
          <el-select
            v-model="form.role_ids"
            multiple
            filterable
            clearable
            placeholder="可选，支持多选"
            class="w-full"
          >
            <el-option
              v-for="role in roles"
              :key="role.id"
              :label="`${role.name}（${role.code}）`"
              :value="role.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_active" />
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
      v-model="assignVisible"
      :title="`分配角色${assignTarget ? ` - ${assignTarget.username}` : ''}`"
      width="min(520px, 92vw)"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-select
        v-model="assignRoleIds"
        multiple
        filterable
        clearable
        placeholder="选择该用户拥有的角色；不选表示不分配任何角色"
        class="w-full"
      >
        <el-option
          v-for="role in roles"
          :key="role.id"
          :label="`${role.name}（${role.code}）`"
          :value="role.id"
        />
      </el-select>
      <p class="mt-2 text-xs text-gray-500">
        提交后会以当前选择覆盖该用户的角色集合。
      </p>
      <template #footer>
        <el-button @click="assignVisible = false">取消</el-button>
        <el-button type="primary" :loading="assigning" @click="onAssign">
          保存
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="resetVisible"
      :title="`重置密码${resetTarget ? ` - ${resetTarget.username}` : ''}`"
      width="min(480px, 92vw)"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form
        ref="resetFormRef"
        :model="resetForm"
        :rules="resetRules"
        label-width="100px"
      >
        <el-form-item label="新密码" prop="password">
          <el-input
            v-model="resetForm.password"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="至少 8 位"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input
            v-model="resetForm.confirm"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="再次输入新密码"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="resetSaving" @click="onResetSubmit">
          重置
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
