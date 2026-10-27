import { defineConfig } from "vitest/config";
import { resolve } from "node:path";

// 独立于 vite.config.ts：单测不加载完整插件链（tailwind/icons/i18n），只做纯逻辑与组件挂载
export default defineConfig({
  resolve: {
    alias: {
      "@": resolve(process.cwd(), "src")
    }
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.spec.ts"],
    reporters: "default"
  }
});
