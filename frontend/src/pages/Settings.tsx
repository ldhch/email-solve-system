import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

interface SystemStatus {
  ai_paused: boolean;
  paused_at: string | null;
  paused_reason: string | null;
  uptime_sec: number;
}

interface NotificationStatus {
  bark_configured: boolean;
  alert_email_configured: boolean;
  alert_email_masked: string | null;
}

export default function Settings() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [notifications, setNotifications] = useState<NotificationStatus | null>(
    null,
  );
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [statusResp, notifResp] = await Promise.all([
        http.get("/system/status"),
        http.get("/system/notifications"),
      ]);
      setStatus(dataOf<SystemStatus>(statusResp));
      setNotifications(dataOf<NotificationStatus>(notifResp));
    } catch {
      setError("加载设置失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function togglePause() {
    if (!status) return;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      if (status.ai_paused) {
        await http.post("/system/resume");
        setSuccess("已恢复 AI 自动回复");
      } else {
        const reason = window.prompt("请输入暂停原因（可选）：") ?? "";
        await http.post("/system/pause", { reason });
        setSuccess("已暂停 AI 自动回复（新邮件只拉取不处理）");
      }
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">设置</h1>
      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}
      {success && <p className="text-sm text-green-600 mb-3">{success}</p>}

      <div className="grid gap-4 md:grid-cols-2">
        <section className="bg-white rounded-lg border border-gray-200 p-4">
          <h2 className="font-semibold mb-3">紧急暂停开关</h2>
          <p className="text-sm text-gray-600 mb-3">
            暂停后新邮件仅拉取入库、不自动回复；恢复后按时间顺序补处理积压邮件。
          </p>
          <div className="flex items-center gap-3">
            <span
              className={`px-3 py-1 rounded text-sm ${
                status?.ai_paused
                  ? "bg-red-100 text-red-700"
                  : "bg-green-100 text-green-700"
              }`}
            >
              {status?.ai_paused ? "已暂停" : "运行中"}
            </span>
            <button
              onClick={togglePause}
              disabled={busy}
              className={`px-4 py-2 rounded text-sm text-white disabled:opacity-50 ${
                status?.ai_paused
                  ? "bg-green-600 hover:bg-green-700"
                  : "bg-red-600 hover:bg-red-700"
              }`}
            >
              {status?.ai_paused ? "恢复自动回复" : "暂停自动回复"}
            </button>
          </div>
          {status?.paused_reason && (
            <p className="text-xs text-gray-500 mt-3">暂停原因：{status.paused_reason}</p>
          )}
          {status?.paused_at && (
            <p className="text-xs text-gray-500 mt-1">
              暂停时间：{status.paused_at.replace("T", " ").replace("Z", "")}
            </p>
          )}
          <p className="text-xs text-gray-400 mt-3">
            已运行 {Math.floor((status?.uptime_sec ?? 0) / 3600)} 小时
            {Math.floor(((status?.uptime_sec ?? 0) % 3600) / 60)} 分钟
          </p>
        </section>

        <section className="bg-white rounded-lg border border-gray-200 p-4">
          <h2 className="font-semibold mb-3">通知设置（只读）</h2>
          <p className="text-sm text-gray-600 mb-3">
            告警通道状态由服务器 .env 配置决定，此处仅展示，不做在线修改。
          </p>
          <ul className="text-sm space-y-2">
            <li className="flex items-center justify-between">
              <span className="text-gray-600">Bark（iOS 推送）</span>
              <span
                className={`px-2 py-0.5 rounded text-xs ${
                  notifications?.bark_configured
                    ? "bg-green-100 text-green-700"
                    : "bg-gray-100 text-gray-600"
                }`}
              >
                {notifications?.bark_configured ? "已配置" : "未配置"}
              </span>
            </li>
            <li className="flex items-center justify-between">
              <span className="text-gray-600">告警邮箱（SMTP）</span>
              <span
                className={`px-2 py-0.5 rounded text-xs ${
                  notifications?.alert_email_configured
                    ? "bg-green-100 text-green-700"
                    : "bg-gray-100 text-gray-600"
                }`}
              >
                {notifications?.alert_email_configured
                  ? notifications.alert_email_masked
                  : "未配置"}
              </span>
            </li>
          </ul>
          <p className="text-xs text-gray-400 mt-3">
            LLM 连续失败 5 次 / IMAP 连续 3 轮失败 / SLA 逾期 / 补偿挽留审核超时
            会自动告警。
          </p>
        </section>
      </div>

      <section className="bg-white rounded-lg border border-gray-200 p-4 mt-4">
        <h2 className="font-semibold mb-2">审计日志</h2>
        <p className="text-sm text-gray-600 mb-3">
          查看系统内全部操作留痕（发送 / 修改 / 删除 / 登录等），支持按动作、操作人、时间筛选。
        </p>
        <Link
          to="/audit"
          className="inline-block px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
        >
          前往审计日志
        </Link>
      </section>
    </Layout>
  );
}
