import { describe, expect, it, vi } from "vitest";

// hooks.ts 依赖 ../index（其中 IconSelect 是 .vue 单文件，vitest 无插件链不解析），
// 这里只测 sanitizeSvg 的清洗语义与渲染分支输出，组件依赖全部 mock 掉。
vi.mock("@/components/ReIcon/index", () => ({
  FontIcon: {},
  IconifyIconOnline: {},
  IconifyIconOffline: {}
}));

import { sanitizeSvg, useRenderIcon } from "@/components/ReIcon/src/hooks";

const SVG_NS = `xmlns="http://www.w3.org/2000/svg"`;
const XLINK_NS = `xmlns:xlink="http://www.w3.org/1999/xlink"`;

describe("sanitizeSvg", () => {
  it("保留正常矢量元素与绘制属性", () => {
    const svg = `<svg ${SVG_NS} viewBox="0 0 24 24"><path d="M4 4h16v16H4z" fill="currentColor"/></svg>`;
    const cleaned = sanitizeSvg(svg);
    expect(cleaned).toContain("<path");
    expect(cleaned).toContain('d="M4 4h16v16H4z"');
    expect(cleaned).toContain('fill="currentColor"');
  });

  it("剥掉事件属性（含 SMIL 动画的 onbegin）", () => {
    const svg = `<svg ${SVG_NS}><animate attributeName="x" onbegin="alert(1)" dur="1s"/><path d="M0 0"/></svg>`;
    const cleaned = sanitizeSvg(svg);
    expect(cleaned).not.toContain("onbegin");
    expect(cleaned).not.toContain("alert");
  });

  it("移除白名单外元素（script/foreignObject/iframe）", () => {
    const svg = `<svg ${SVG_NS}><script>alert(1)</script><foreignObject><p>x</p></foreignObject><iframe src="https://evil"></iframe><path d="M0 0"/></svg>`;
    const cleaned = sanitizeSvg(svg);
    expect(cleaned?.toLowerCase()).not.toContain("script");
    expect(cleaned?.toLowerCase()).not.toContain("foreignobject");
    expect(cleaned?.toLowerCase()).not.toContain("iframe");
    expect(cleaned).not.toContain("alert");
  });

  it("移除指向外部的 href/xlink:href，保留 # 片段引用", () => {
    const svg = `<svg ${SVG_NS} ${XLINK_NS}><use href="javascript:alert(1)"/><a xlink:href="https://evil.example"><path d="M0 0"/></a><use href="#icon"/></svg>`;
    const cleaned = sanitizeSvg(svg);
    expect(cleaned).not.toContain("javascript:");
    expect(cleaned).not.toContain("evil.example");
    expect(cleaned).toContain('#icon');
  });

  it("解析失败、根节点不是 svg 或缺 DOM API 时返回 null", () => {
    expect(sanitizeSvg("<html><body>hi</body></html>")).toBeNull();
    expect(sanitizeSvg("<svg><b>bold")).toBeNull();
  });

  it("style 属性一律移除", () => {
    const svg = `<svg ${SVG_NS}><path d="M0 0" style="fill:red"/></svg>`;
    const cleaned = sanitizeSvg(svg);
    expect(cleaned).not.toContain("style");
  });
});

describe("useRenderIcon 的 SVG 字符串分支", () => {
  it("白名单清洗后的 svg 渲染时 innerHTML 不含事件属性", () => {
    const icon = `<svg ${SVG_NS}><animate onbegin="alert(1)" dur="1s"/><path d="M0 0"/></svg>`;
    const comp = useRenderIcon(icon) as { render: () => { props?: Record<string, unknown> } };
    const vnode = comp.render();
    const html = String(vnode.props?.innerHTML ?? "");
    expect(html).not.toContain("onbegin");
    expect(html).toContain("<path");
  });

  it("解析失败的字符串渲染为不含 innerHTML 的空白 span", () => {
    const icon = `<svg><animate onbegin="alert(1)" dur="1s`;
    const comp = useRenderIcon(icon) as { render: () => { props?: Record<string, unknown> } };
    const vnode = comp.render();
    expect(vnode.props?.innerHTML).toBeUndefined();
  });
});
