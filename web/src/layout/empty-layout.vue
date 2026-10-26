<script setup lang="ts">
/**
 * 目录路由的容器。
 *
 * 后端动态菜单里的目录（component 为空、带 children）只需要一个
 * `<router-view />` 占位；若复用完整 `Layout`，页面里会再套一层侧边栏
 * 和标签页。这里刻意保持最小。
 */
defineOptions({ name: "EmptyLayout" });
</script>

<template>
  <router-view />
</template>
