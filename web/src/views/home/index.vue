<script setup lang="ts">
import { computed } from "vue";
import { useGlobal } from "@pureadmin/utils";

defineOptions({ name: "Home" });

const { $config } = useGlobal<GlobalPropertiesApi>();

/** 构建信息来自 `build/utils.ts` 注入的 `__APP_INFO__` */
const { name, version, dependencies } = __APP_INFO__.pkg;

/** 开发期由 vite 代理转发，生产期由 Nginx 反代到同一个 `/api` 前缀 */
const apiProxy = "/api → http://127.0.0.1:8000";

const backendDeps = computed(() =>
  Object.entries(dependencies ?? {})
    .filter(([dep]) =>
      ["vue", "vue-router", "pinia", "element-plus", "axios"].includes(dep)
    )
    .map(([dep, ver]) => `${dep} ${ver}`)
);
</script>

<template>
  <div class="p-4">
    <el-card shadow="never">
      <template #header>
        <span class="font-medium">{{ $config?.Title ?? name }} {{ version }}</span>
      </template>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="前端骨架">
          派生自 vue-pure-admin（MIT），业务屏按里程碑逐步接入
        </el-descriptions-item>
        <el-descriptions-item label="接口代理">{{ apiProxy }}</el-descriptions-item>
        <el-descriptions-item label="已接入依赖">
          {{ backendDeps.join("、") }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>
