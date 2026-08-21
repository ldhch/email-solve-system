import axios from "axios";

export const http = axios.create({
  baseURL: "/api/v1",
  withCredentials: true,
  timeout: 60000,
});

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (
      error.response?.status === 401 &&
      !window.location.pathname.startsWith("/login")
    ) {
      window.location.href = "/login";
    }
    return Promise.reject(error);
  },
);

export interface ApiEnvelope<T> {
  code: number;
  data: T;
  msg: string;
}

export function dataOf<T>(resp: { data: ApiEnvelope<T> }): T {
  return resp.data.data;
}

export function errorText(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: string } } })?.response
    ?.data?.detail;
  switch (detail) {
    case "LLM_FAILED":
      return "AI 调用失败，请稍后再试";
    case "SMTP_FAILED":
      return "邮件发送失败（SMTP）";
    case "INVALID_CREDENTIALS":
      return "用户名或密码错误";
    case "ACCOUNT_LOCKED":
      return "账号已锁定，请 30 分钟后再试";
    case "RATE_LIMITED":
      return "尝试过于频繁，请稍后再试";
    case "NOT_REVIEWABLE":
      return "该草稿当前不可审核";
    case "NOT_EDITABLE":
      return "该回复当前不可编辑";
    case "TOO_LONG":
      return "内容过长（最多 5000 字）";
    case "UNSUPPORTED_TYPE":
      return "不支持的文件类型（仅 PDF / DOCX / MD）";
    case "TOO_LARGE":
      return "文件超过 20MB 上限";
    case "EMPTY_CONTENT":
      return "未能从文件中提取到文本";
    case "NOTHING_TO_SPLIT":
      return "该会话邮件不足，无法拆分";
    case "EMAIL_NOT_IN_CONVERSATION":
      return "所选邮件不在当前会话";
    case "SAME_CONVERSATION":
      return "不能合并到当前会话自身";
    case "DIFFERENT_CUSTOMER":
      return "两个会话不是同一客户，无法合并";
    case "EMPTY_WHITELIST":
      return "开启测试模式需要至少一个白名单发件人";
    case "INVALID_EMAIL":
      return "白名单中存在无效邮箱格式";
    case "NOT_FOUND":
      return "资源不存在";
    default:
      return "操作失败，请重试";
  }
}
