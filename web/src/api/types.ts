/**
 * 认证接口的响应信封。
 *
 * 注意：后端只有 `/api/login`、`/api/refresh-token`、`/api/auth/routes`、`/api/logout`
 * 这四个认证接口返回 `{code, message, data}` 信封；其余业务接口是纯 REST 对象，
 * 错误统一为 `{"detail": "..."}`（见 `service/README.md`），不要对业务响应做拆信封处理。
 */
export type Envelope<T> = { code: number; message: string; data: T };

export type LoginData = {
  avatar: string | null;
  username: string;
  nickname: string;
  roles: string[];
  permissions: string[];
  accessToken: string;
  refreshToken: string;
  /** epoch 毫秒，写进 authorized-token cookie 的 expires */
  expires: number;
};

export type RefreshData = Pick<
  LoginData,
  "accessToken" | "refreshToken" | "expires"
>;

/** FastAPI 的业务错误体 */
export type ApiError = { detail?: string };
