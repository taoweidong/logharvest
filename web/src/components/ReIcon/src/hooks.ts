import type { iconType } from "./types";
import { h, defineComponent, type Component } from "vue";
import { FontIcon, IconifyIconOnline, IconifyIconOffline } from "../index";
import { OFFLINE_ICON_NAMES } from "./offlineIconNames";

/**
 * 支持 `iconfont`、自定义 `svg` 以及 `iconify` 中所有的图标
 * @see 点击查看文档图标篇 {@link https://pure-admin.cn/pages/icon/}
 * @param icon 必传 图标
 * @param attrs 可选 iconType 属性
 * @returns Component
 */
export function useRenderIcon(icon: any, attrs?: iconType): Component {
  // iconfont
  const ifReg = /^IF-/;
  // typeof icon === "function" 属于SVG
  if (ifReg.test(icon)) {
    // iconfont
    const name = icon.split(ifReg)[1];
    const iconName = name.slice(
      0,
      name.indexOf(" ") == -1 ? name.length : name.indexOf(" ")
    );
    const iconType = name.slice(name.indexOf(" ") + 1, name.length);
    return defineComponent({
      name: "FontIcon",
      render() {
        return h(FontIcon, {
          icon: iconName,
          iconType,
          ...attrs
        });
      }
    });
  } else if (typeof icon === "function" || typeof icon?.render === "function") {
    // svg
    return attrs ? h(icon, { ...attrs }) : icon;
  } else if (typeof icon === "object") {
    return defineComponent({
      name: "OfflineIcon",
      render() {
        return h(IconifyIconOffline, {
          icon: icon,
          ...attrs
        });
      }
    });
  } else {
    /*
     * 字符串图标名（如 `ri:settings-3-line` / `ep:menu`）。
     *
     * 原逻辑：含 ":" 就用在线组件。但后端菜单图标恰好都是冒号格式，
     * 于是每次渲染菜单都会去 api.iconify.design 拉数据——弱网/内网下首屏
     * 被拖到 20s+，甚至请求挂起。
     *
     * 现逻辑：先把 ":" 归一成 "/"（本地注册的命名形式），若该名字已在
     * offlineIconNames 中注册就用离线组件；**未注册才回退在线**，
     * 保证管理员在图标选择器里选到未预置的图标时仍能显示，不会变空白。
     */
    return defineComponent({
      name: "Icon",
      render() {
        if (!icon) return;
        const offlineName = String(icon).replace(":", "/");
        const useOffline =
          !String(icon).includes(":") ||
          OFFLINE_ICON_NAMES.includes(offlineName);
        return h(useOffline ? IconifyIconOffline : IconifyIconOnline, {
          icon: useOffline ? offlineName : icon,
          ...attrs
        });
      }
    });
  }
}
