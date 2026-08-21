import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

interface SystemStatus {
  ai_paused: boolean;
  paused_at: string | null;
  paused_reason: string | null;
  uptime_sec: number;
  test_mode: boolean;
  test_whitelist: string[];
}

interface NotificationStatus {
  bark_configured: boolean;
  alert_email_configured: boolean;
  alert_email_masked: string | null;
}

/** Human-friendly uptime, e.g. "35 秒" / "12 分钟" / "2 小时 5 分钟" / "3 天 4 小时". */
function fmtUptime(sec: number): string {
  if (sec < 60) return `${sec} 秒`;
  const totalMin = Math.floor(sec / 60);
  if (totalMin < 60) return `${totalMin} 分钟`;
  const hours = Math.floor(totalMin / 60);
  const minutes = totalMin % 60;
  if (hours < 24) return `${hours} 小时 ${minutes} 分钟`;
  const days = Math.floor(hours / 24);
  return `${days} 天 ${hours % 24} 小时`;
}

export default function Settings() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [notifications, setNotifications] = useState<NotificationStatus | null>(
    null,
  );
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  const [testMode, setTestMode] = useState(false);
  const [whitelistText, setWhitelistText] = useState("");

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

  // Keep the test-mode editor in sync with the server once status loads.
  useEffect(() => {
    if (!status) return;
    setTestMode(status.test_mode);
    setWhitelistText(status.test_whitelist.join("\n"));
  }, [status]);

  async function saveTestMode() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const whitelist = whitelistText
        .split(/[\n,]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      await http.put("/system/test-mode", { enabled: testMode, whitelist });
      setSuccess(testMode ? "已开启测试模式" : "已关闭测试模式");
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function setMode(mode: "running" | "paused") {
    if (!status) return;
    if (
      (mode === "running" && !status.ai_paused) ||
      (mode === "paused" && status.ai_paused)
    ) {
      return; // already in this mode
    }
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      if (mode === "paused") {
        await http.post("/system/pause", { reason: "" });
        setSuccess("已暂停：新邮件只拉取、不自动回复");
      } else {
        await http.post("/system/resume");
        setSuccess("已恢复自动回复");
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
          <p className="text-sm text-gray-600 mb-4">
            暂停后新邮件仅拉取入库、不自动回复；恢复后按时间顺序补处理积压邮件。
          </p>

          {/* Mode toggle: active segment = blue, inactive = white */}
          <div className="flex rounded-lg border border-gray-300 overflow-hidden w-72">
            <button
              type="button"
              onClick={() => setMode("running")}
              disabled={busy}
              className={`flex-1 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${
                status && !status.ai_paused
                  ? "bg-blue-600 text-white"
                  : "bg-white text-gray-600 hover:bg-blue-50"
              }`}
            >
              运行中
            </button>
            <button
              type="button"
              onClick={() => setMode("paused")}
              disabled={busy}
              className={`flex-1 py-2 text-sm font-medium transition-colors border-l border-gray-300 disabled:opacity-50 ${
                status?.ai_paused
                  ? "bg-blue-600 text-white"
                  : "bg-white text-gray-600 hover:bg-blue-50"
              }`}
            >
              已暂停
            </button>
          </div>

          <p className="text-xs text-gray-500 mt-3">
            当前状态：
            {status?.ai_paused ? (
              <span className="text-red-600 font-medium">已暂停</span>
            ) : (
              <span className="text-green-600 font-medium">运行中</span>
            )}
            {status?.paused_reason && ` · 原因：${status.paused_reason}`}
          </p>
          {status?.paused_at && (
            <p className="text-xs text-gray-500 mt-1">
              暂停时间：{status.paused_at.replace("T", " ").replace("Z", "")}
            </p>
          )}
        </section>

        <section className="bg-white rounded-lg border border-gray-200 p-4">
          <h2 className="font-semibold mb-3">通知告警（自动）</h2>
          <p className="text-xs text-gray-500 mb-3">
            系统会自动通知你以下情况：LLM 连续失败 5 次 / IMAP 连续 3 轮失败 /
            SLA 逾期 / 补偿挽留审核超时。
          </p>
          <details className="text-sm">
            <summary className="w-fit cursor-pointer select-none text-xs text-gray-400 hover:text-gray-600">
              系统状态（高级）
            </summary>
            <ul className="mt-3 space-y-2">
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
              <li className="flex items-center justify-between text-gray-500">
                <span>服务进程已运行</span>
                <span className="tabular-nums">
                  {fmtUptime(status?.uptime_sec ?? 0)}
                </span>
              </li>
            </ul>
          </details>
        </section>
      </div>

      <section className="bg-white rounded-lg border border-gray-200 p-4 mt-4">
        <h2 className="font-semibold mb-3">测试模式（发件人白名单）</h2>
        <p className="text-sm text-gray-600 mb-4">
          开启后系统只自动处理白名单中的发件人；其余未读邮件保持原样（不拉取入库、不回复、不翻译），关闭后恢复正常处理。
        </p>

        <div className="flex rounded-lg border border-gray-300 overflow-hidden w-72">
          <button
            type="button"
            onClick={() => setTestMode(false)}
            disabled={busy}
            className={`flex-1 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${
              !testMode
                ? "bg-blue-600 text-white"
                : "bg-white text-gray-600 hover:bg-blue-50"
            }`}
          >
            正式运行
          </button>
          <button
            type="button"
            onClick={() => setTestMode(true)}
            disabled={busy}
            className={`flex-1 py-2 text-sm font-medium transition-colors border-l border-gray-300 disabled:opacity-50 ${
              testMode
                ? "bg-blue-600 text-white"
                : "bg-white text-gray-600 hover:bg-blue-50"
            }`}
          >
            测试模式
          </button>
        </div>

        <label className="block mt-4 mb-1 text-sm text-gray-600">
          白名单发件人（每行或逗号分隔一个邮箱）
        </label>
        <textarea
          value={whitelistText}
          onChange={(e) => setWhitelistText(e.target.value)}
          rows={4}
          placeholder={"419018463@qq.com"}
          className="w-full border border-gray-300 rounded text-sm p-2 font-mono"
        />

        <div className="flex items-center justify-between mt-3">
          <p className="text-xs text-gray-500">
            当前状态：
            {testMode ? (
              <span className="text-orange-600 font-medium">测试模式</span>
            ) : (
              <span className="text-green-600 font-medium">正式运行</span>
            )}
          </p>
          <button
            type="button"
            onClick={saveTestMode}
            disabled={busy}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
          >
            保存测试模式设置
          </button>
        </div>
      </section>

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
