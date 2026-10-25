import { getToken } from "@/utils/auth";
import type { ProgressEvent } from "@/api/logharvestTypes";

/**
 * SSE 进度流读取。
 *
 * 为什么不用 `EventSource`：后端的 `/api/jobs/{id}/events` 要求
 * `Authorization: Bearer <accessToken>`，而 `EventSource` 不能自定义请求头，
 * 把 token 塞进 query string 又会泄露到访问日志/历史记录，
 * 所以这里用 `fetch` + `ReadableStream` 手工读流。
 */

/** 后端一帧的形状：`event: progress\ndata: {...}\n\n`（多行 data 不出现，见 routes_events.py） */
const FRAME_SEPARATOR = "\n\n";
const DATA_PREFIX = "data: ";

/**
 * 解析缓冲区里**已经完整**的帧（以 `\n\n` 结尾），返回解析出的事件和留在缓冲区的不完整尾部。
 *
 * 边界约定：
 * - 只有以 `\n\n` 收尾的帧才会被解析，最后一段（可能还在传输中）原样放进 `rest`
 * - 只取帧内第一条 `data: ` 行作为载荷（后端每帧恰好一条）
 * - 载荷不是合法 JSON 时抛错，由调用方决定降级策略（作业详情页回退为轮询）
 */
export function parseSseChunk(buffer: string): {
  events: ProgressEvent[];
  rest: string;
} {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames = normalized.split(FRAME_SEPARATOR);
  const rest = frames.pop() ?? "";
  const events: ProgressEvent[] = [];
  for (const frame of frames) {
    const dataLine = frame
      .split("\n")
      .find(line => line.startsWith(DATA_PREFIX));
    if (!dataLine) continue;
    events.push(JSON.parse(dataLine.slice(DATA_PREFIX.length)) as ProgressEvent);
  }
  return { events, rest };
}

/**
 * 订阅某个作业的进度流。
 *
 * - `signal` 中止时 `fetch`/`reader.read()` 会抛出 `AbortError`，调用方在 `onBeforeUnmount` 里 abort
 * - 后端在作业进入终态后发送最后一个快照并结束流，此时返回最后一个事件
 * - **返回 `undefined` 表示流在没有观察到终态快照时就结束了**（作业被删除、
 *   代理切断、连接被中途关闭）。调用方必须把它当成"需要回退轮询"，
 *   不能当成"作业已完成"。
 * - 非 2xx（如 401/404）抛错，由调用方提示并回退到轮询
 */
export async function streamJobProgress(
  jobId: number,
  onEvent: (event: ProgressEvent) => void,
  signal: AbortSignal
): Promise<ProgressEvent | undefined> {
  const token = getToken();
  const response = await fetch(`/api/jobs/${jobId}/events`, {
    headers: {
      Accept: "text/event-stream",
      Authorization: `Bearer ${token?.accessToken ?? ""}`
    },
    signal
  });
  if (!response.ok || !response.body) {
    throw new Error(`progress stream failed: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let last: ProgressEvent | undefined;
  const emit = (events: ProgressEvent[]) => {
    events.forEach(event => {
      last = event;
      onEvent(event);
    });
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.rest;
    emit(parsed.events);
  }
  // 收尾：flush 可能剩下的半个多字节字符，并尝试解析最后一帧
  // （服务端在终止前发的帧可能没有以空行结尾，不补这一步就会整帧丢弃）。
  buffer += decoder.decode();
  if (buffer.trim()) {
    try {
      emit(parseSseChunk(`${buffer}\n\n`).events);
    } catch {
      // 尾部残留不是完整 JSON（代理截断、心跳注释行等）：宁可少一帧，
      // 也不能让一次正常关流变成异常路径，把用户引到"实时中断"的假提示上。
    }
  }
  return last;
}
