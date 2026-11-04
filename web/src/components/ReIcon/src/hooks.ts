import type { iconType } from "./types";
import { h, defineComponent, type Component } from "vue";
import { FontIcon, IconifyIconOnline, IconifyIconOffline } from "../index";
import { OFFLINE_ICON_NAMES } from "./offlineIconNames";

/** 原始 `<svg>` 字符串：允许直接把后端下发或富文本里的 svg 当图标渲染 */
const svgReg = /^\s*<svg[\s>]/;
const imgReg = /^(https?:\/\/|\/\/|data:image\/)/;
/** 清洗结果缓存，避免同一串 svg 在每次渲染时重复跑正则 */
const svgCache = new Map<string, string>();

/**
 * SVG 绘制元素白名单：只保留纯矢量绘制所需的标签。
 * 刻意排除 script / foreignObject（可嵌 HTML）、iframe/object/embed、
 * image（外链资源可外泄内网地址）以及 animate/set/animateMotion/
 * animateTransform（SMIL 动画元素可携带 onbegin 等事件属性）。
 */
const SVG_ALLOWED_TAGS = new Set([
  "svg",
  "g",
  "defs",
  "symbol",
  "use",
  "title",
  "desc",
  "metadata",
  "path",
  "rect",
  "circle",
  "ellipse",
  "line",
  "polyline",
  "polygon",
  "text",
  "tspan",
  "textPath",
  "clipPath",
  "mask",
  "pattern",
  "marker",
  "linearGradient",
  "radialGradient",
  "stop",
  "filter",
  "feBlend",
  "feColorMatrix",
  "feComponentTransfer",
  "feComposite",
  "feConvolveMatrix",
  "feDiffuseLighting",
  "feDisplacementMap",
  "feDistantLight",
  "feDropShadow",
  "feFlood",
  "feFuncA",
  "feFuncB",
  "feFuncG",
  "feFuncR",
  "feGaussianBlur",
  "feImage",
  "feMerge",
  "feMergeNode",
  "feMorphology",
  "feOffset",
  "fePointLight",
  "feSpecularLighting",
  "feSpotLight",
  "feTile",
  "feTurbulence"
]);

/**
 * SVG 字符串白名单清洗：菜单 icon 是管理端自由文本，会随 /api/auth/routes
 * 下发给所有可见该菜单的用户并在侧边栏以 innerHTML 渲染，属于存储型 XSS
 * 入口。这里用 DOMParser（inert 文档，解析期不执行脚本）按白名单裁剪元素、
 * 剥掉事件属性与指向外部的 href/xlink:href，再序列化回字符串。
 * 解析失败、根节点不是 svg 或环境缺 DOM API 时返回 null（调用方不渲染 HTML）。
 */
export function sanitizeSvg(svg: string): string | null {
  if (typeof DOMParser === "undefined" || typeof XMLSerializer === "undefined") {
    return null;
  }
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  if (doc.getElementsByTagName("parsererror").length > 0) return null;
  const root = doc.documentElement;
  if (!root || root.nodeName.toLowerCase() !== "svg") return null;

  const prune = (el: Element) => {
    for (const child of [...el.children]) {
      if (SVG_ALLOWED_TAGS.has(child.nodeName.toLowerCase())) {
        prune(child);
      } else {
        child.remove();
      }
    }
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      if (
        name.startsWith("on") ||
        ((name === "href" || name === "xlink:href") && !attr.value.startsWith("#")) ||
        name === "style"
      ) {
        el.removeAttribute(attr.name);
      }
    }
  };
  prune(root);
  return new XMLSerializer().serializeToString(root);
}

/**
 * 支持 `iconfont`、`SVG` 字符串、`SVG` 函数组件、图片 `URL`
 * 以及 `iconify` 中所有的图标
 * @see 点击查看文档图标篇 {@link https://pure-admin.cn/pages/icon/}
 * @param icon 必传 图标
 * @param attrs 可选 iconType 属性
 * @returns Component
 */
export function useRenderIcon(icon: any, attrs?: iconType): Component {
  // iconfont
  const ifReg = /^IF-/;
  // typeof icon === "function" 属于SVG
  if (typeof icon === "string" && svgReg.test(icon)) {
    // SVG 字符串：先按白名单清洗（见 sanitizeSvg），再去掉写死的宽高，
    // 交由外层 class / attrs 控制尺寸。清洗失败说明含可执行或白名单外内容，
    // 渲染为空白，绝不让未消毒的字符串进 innerHTML。
    let cleanedSvg = svgCache.get(icon);
    if (cleanedSvg === undefined) {
      if (svgCache.size > 200) svgCache.clear();
      const sanitized = sanitizeSvg(icon);
      if (sanitized === null) {
        svgCache.set(icon, "");
        return defineComponent({
          name: "SvgRawIcon",
          render: () => h("span", { class: "svg-raw-icon", ...attrs })
        });
      }
      cleanedSvg = sanitized.replace(/<svg([^>]*)>/, (_, a) =>
        `<svg${a.replace(/\s*(width|height)="[^"]*"/g, "")}>`
      );
      svgCache.set(icon, cleanedSvg);
    }
    if (cleanedSvg === "") {
      return defineComponent({
        name: "SvgRawIcon",
        render: () => h("span", { class: "svg-raw-icon", ...attrs })
      });
    }
    return defineComponent({
      name: "SvgRawIcon",
      render: () =>
        h("span", {
          class: "svg-raw-icon",
          innerHTML: cleanedSvg,
          ...attrs
        })
    });
  } else if (typeof icon === "string" && imgReg.test(icon)) {
    // 图片 URL
    return defineComponent({
      name: "ImgIcon",
      render: () =>
        h("img", {
          src: icon,
          alt: "",
          style: {
            width: "18px",
            height: "18px",
            minWidth: "18px",
            objectFit: "contain"
          },
          ...attrs
        })
    });
  } else if (ifReg.test(icon)) {
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
