// 类型化 API 客户端（DD-14 §7）：Bearer token、JSON、401 统一登出、ApiError。
import { API_PREFIX, TOKEN_KEY } from '../utils/constants';

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

interface ApiErrorBody {
  detail?: unknown;
}

function messageOf(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message);
  }
  return '请求失败';
}

export async function apiFetch<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  const hadToken = !!token;
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const resp = await fetch(`${API_PREFIX}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) {
    // 登录请求本身 401（无 token）不上抛跳转，由登录页行内提示后端 detail；
    // 携带 token 的 401（过期/失效）才清 token 跳登录页。
    clearToken();
    if (hadToken && !location.pathname.startsWith('/login')) {
      location.href = '/login';
    }
    let detail: unknown;
    try {
      detail = ((await resp.json()) as ApiErrorBody).detail;
    } catch {
      detail = undefined;
    }
    const msg =
      hadToken ? '未登录或登录已过期' : messageOf(detail);
    throw new ApiError(401, msg, detail);
  }
  if (!resp.ok) {
    let detail: unknown;
    try {
      detail = ((await resp.json()) as ApiErrorBody).detail;
    } catch {
      detail = undefined;
    }
    throw new ApiError(resp.status, messageOf(detail), detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

// multipart 上传（T6 评测集导入 zip 等；FormData 自带 boundary，勿设 Content-Type）
export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const resp = await fetch(`${API_PREFIX}${path}`, {
    method: 'POST',
    headers,
    body: form,
  });
  if (!resp.ok) {
    let detail: unknown;
    try {
      detail = ((await resp.json()) as ApiErrorBody).detail;
    } catch {
      detail = undefined;
    }
    throw new ApiError(resp.status, messageOf(detail), detail);
  }
  return (await resp.json()) as T;
}
