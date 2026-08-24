import { useCallback, useEffect, useState } from "react";
import { dataOf, http } from "../api/client";
import { Layout } from "../components/Layout";

interface AuditItem {
  id: number;
  actor_id: number | null;
  actor_name: string | null;
  action: string;
  resource_type: string;
  resource_id: number;
  ip: string | null;
  at: string;
}

const ACTION_LABEL: Record<string, string> = {
  login: "登录",
  logout: "退出登录",
  login_failed: "登录失败",
  pause: "暂停自动回复",
  resume: "恢复自动回复",
  test_mode_changed: "测试模式变更",
  reply_sent: "回复已发送",
  reply_approved: "审核通过",
  reply_rejected: "驳回草稿",
  reply_edited: "编辑回复",
  reply_failed: "发送失败",
  manual_reply_sent: "人工回复已发送",
  ticket_created: "创建工单",
  ticket_resolved: "解决工单",
  ticket_updated: "更新工单",
  sla_overdue: "SLA 逾期",
  retention_auto_released: "自动放行退货",
  ack_sent: "已发送回执",
  ack_failed: "回执发送失败",
  kb_uploaded: "知识库上传",
  kb_deleted: "知识库删除",
  qa_created: "新增问答",
  qa_updated: "更新问答",
  qa_deleted: "删除问答",
  qa_bulk_import: "批量导入问答",
  blocked_sender_created: "加入黑名单",
  blocked_sender_deleted: "解除黑名单",
  template_created: "新增模板",
  template_updated: "更新模板",
  template_deleted: "删除模板",
};

export default function AuditLogs() {
  const [items, setItems] = useState<AuditItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [action, setAction] = useState("");
  const [actorId, setActorId] = useState("");
  const [fromAt, setFromAt] = useState("");
  const [toAt, setToAt] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number> = { page, size: 20 };
      if (action.trim()) params.action = action.trim();
      if (actorId.trim()) params.actor_id = Number(actorId.trim());
      if (fromAt) params.from = new Date(fromAt).toISOString();
      if (toAt) params.to = new Date(toAt).toISOString();
      const resp = await http.get("/audit-logs", { params });
      const data = dataOf<{ items: AuditItem[]; total: number }>(resp);
      setItems(data.items);
      setTotal(data.total);
    } catch {
      // keep the previous list on failure
    } finally {
      setLoading(false);
    }
  }, [page, action, actorId, fromAt, toAt]);

  useEffect(() => {
    load();
  }, [load]);

  function reset() {
    setAction("");
    setActorId("");
    setFromAt("");
    setToAt("");
    setPage(1);
  }

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">审计日志</h1>
      <p className="text-sm text-gray-500 mb-4">
        系统内所有发送 / 修改 / 删除 / 登录等操作均留痕（按时间倒序）。
      </p>

      <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4 flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">动作</label>
          <input
            value={action}
            onChange={(e) => {
              setAction(e.target.value);
              setPage(1);
            }}
            placeholder="如 login / pause / reply_sent"
            className="border border-gray-300 rounded px-3 py-1.5 text-sm w-52"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">操作人 ID</label>
          <input
            value={actorId}
            onChange={(e) => {
              setActorId(e.target.value);
              setPage(1);
            }}
            placeholder="留空 = 全部（AI 操作无 ID）"
            className="border border-gray-300 rounded px-3 py-1.5 text-sm w-40"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">开始时间</label>
          <input
            type="datetime-local"
            value={fromAt}
            onChange={(e) => {
              setFromAt(e.target.value);
              setPage(1);
            }}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">结束时间</label>
          <input
            type="datetime-local"
            value={toAt}
            onChange={(e) => {
              setToAt(e.target.value);
              setPage(1);
            }}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <button
          onClick={reset}
          className="px-3 py-1.5 border border-gray-300 rounded text-sm text-gray-600"
        >
          清空筛选
        </button>
      </div>

      {loading ? (
        <p className="text-gray-500">加载中…</p>
      ) : items.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-gray-500">
          暂无审计记录
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-4 py-2">时间</th>
                <th className="px-4 py-2">动作</th>
                <th className="px-4 py-2">对象</th>
                <th className="px-4 py-2">对象 ID</th>
                <th className="px-4 py-2">操作人</th>
                <th className="px-4 py-2">IP</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className="border-t border-gray-100">
                  <td className="px-4 py-2 text-gray-500">
                    {item.at.replace("T", " ").replace("Z", "")}
                  </td>
                  <td className="px-4 py-2 font-medium">
                    {ACTION_LABEL[item.action] || item.action}
                  </td>
                  <td className="px-4 py-2 text-gray-600">{item.resource_type}</td>
                  <td className="px-4 py-2 text-gray-600">{item.resource_id}</td>
                  <td className="px-4 py-2 text-gray-600">
                    {item.actor_name ||
                      (item.actor_id === null ? "AI 自动" : `用户 #${item.actor_id}`)}
                  </td>
                  <td className="px-4 py-2 text-gray-500">{item.ip || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex items-center justify-between px-4 py-3 border-t border-gray-200 text-sm text-gray-600">
            <span>共 {total} 条</span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="px-3 py-1 border border-gray-300 rounded disabled:opacity-40"
              >
                上一页
              </button>
              <span className="self-center">第 {page} 页</span>
              <button
                disabled={page * 20 >= total}
                onClick={() => setPage((p) => p + 1)}
                className="px-3 py-1 border border-gray-300 rounded disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          </div>
        </div>
      )}
    </Layout>
  );
}
