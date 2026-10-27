/**
 * token 刷新期间"暂存待重放请求"的队列。
 *
 * 无感刷新的契约：token 过期时，触发刷新的那批请求先排队等新 token；
 * 刷新成功逐个补上 Authorization 重放，**刷新失败必须逐个 reject**——
 * 此时请求尚未发出，axios 超时不会生效，只清空队列不 reject 就意味着
 * 调用方的 `await` 永久挂起、页面 loading 永不消失（2026-10-28 修复的缺陷）。
 *
 * 独立成模块是为了可单测：队列行为（重放顺序、失败 reject、重复刷新合并）
 * 不该绑定在 axios 拦截器内部才能验证。
 */

type PendingRequest = {
  onRefreshed: (token: string) => void;
  onRejected: (reason: unknown) => void;
};

let pending: PendingRequest[] = [];

/** 排队等待刷新结果；返回的 Promise 在重放时 resolve、刷新失败时 reject。 */
export function enqueuePendingRequest<T>(
  attach: (token: string) => T
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    pending.push({
      onRefreshed: token => resolve(attach(token)),
      onRejected: reject
    });
  });
}

/** 刷新成功：按排队顺序逐个补 token 重放，并清空队列。 */
export function flushPendingRequests(token: string): void {
  for (const request of pending) {
    request.onRefreshed(token);
  }
  pending = [];
}

/** 刷新失败：逐个 reject（把失败传回各调用方），再清空队列。 */
export function rejectPendingRequests(reason: unknown): void {
  for (const request of pending) {
    request.onRejected(reason);
  }
  pending = [];
}

/** 当前排队中的请求数（测试与诊断用）。 */
export function pendingRequestCount(): number {
  return pending.length;
}

/** 清空队列（不通知等待者）；仅供测试隔离使用。 */
export function clearPendingRequests(): void {
  pending = [];
}
