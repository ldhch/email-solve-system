import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

interface TicketItem {
  id: number;
  conversation_id: number;
  summary_cn: string;
  sla_deadline: string | null;
  risk_level: string;
  status: string;
  age_minutes: number;
  is_overdue: boolean;
}

export default function Tickets() {
  const [items, setItems] = useState<TicketItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<"pending" | "in_progress" | "resolved">("pending");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const navigate = useNavigate();

  const load = useCallback(async () => {
    try {
      const resp = await http.get("/tickets", { params: { status: filter, page, size: 20 } });
      const data = dataOf<{ items: TicketItem[]; total: number }>(resp);
      setItems(data.items);
      setTotal(data.total);
    } catch {
      setError("加载工单失败");
    }
  }, [filter, page]);

  useEffect(() => {
    load();
  }, [load]);

  async function start(ticket: TicketItem) {
    setBusyId(ticket.id);
    setError("");
    try {
      await http.patch(`/tickets/${ticket.id}`, { status: "in_progress" });
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyId(null);
    }
  }

  async function resolve(ticket: TicketItem) {
    const ownerReply = window.prompt("请填写给客户的中文回复（解决工单必填）：");
    if (ownerReply === null) return;
    if (!ownerReply.trim()) {
      setError("必须填写中文回复才能解决工单");
      return;
    }
    setBusyId(ticket.id);
    setError("");
    try {
      await http.patch(`/tickets/${ticket.id}`, {
        status: "resolved",
        owner_reply_cn: ownerReply.trim(),
      });
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyId(null);
    }
  }

  const tabs = [
    { key: "pending" as const, label: "待处理" },
    { key: "in_progress" as const, label: "处理中" },
    { key: "resolved" as const, label: "已解决" },
  ];

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">工单（高风险）</h1>
      <div className="flex gap-2 mb-4">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => {
              setFilter(t.key);
              setPage(1);
            }}
            className={`px-3 py-1.5 rounded text-sm ${
              filter === t.key
                ? "bg-blue-600 text-white"
                : "bg-white border border-gray-300 text-gray-600"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}
      {items.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-gray-500">
          暂无工单
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-4 py-2">摘要</th>
                <th className="px-4 py-2">SLA 截止</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2">年龄</th>
                <th className="px-4 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr
                  key={t.id}
                  className="border-t border-gray-100 hover:bg-blue-50"
                >
                  <td
                    className="px-4 py-2 font-medium cursor-pointer"
                    onClick={() => navigate(`/conversations/${t.conversation_id}`)}
                  >
                    {t.summary_cn || "—"}
                  </td>
                  <td className="px-4 py-2">
                    <span className={t.is_overdue ? "text-red-600 font-medium" : ""}>
                      {t.sla_deadline
                        ? t.sla_deadline.replace("T", " ").replace("Z", "")
                        : "—"}
                    </span>
                    {t.is_overdue && (
                      <span className="ml-2 px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-xs">
                        已逾期
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-gray-600">{t.status}</td>
                  <td className="px-4 py-2 text-gray-500">{t.age_minutes} 分钟</td>
                  <td className="px-4 py-2">
                    <div className="flex gap-2">
                      <button
                        onClick={() => navigate(`/conversations/${t.conversation_id}`)}
                        className="px-2 py-1 border border-gray-300 rounded text-xs"
                      >
                        查看会话
                      </button>
                      {t.status === "pending" && (
                        <button
                          disabled={busyId === t.id}
                          onClick={() => start(t)}
                          className="px-2 py-1 bg-blue-600 text-white rounded text-xs disabled:opacity-50"
                        >
                          开始处理
                        </button>
                      )}
                      {t.status !== "resolved" && (
                        <button
                          disabled={busyId === t.id}
                          onClick={() => resolve(t)}
                          className="px-2 py-1 bg-green-600 text-white rounded text-xs disabled:opacity-50"
                        >
                          解决
                        </button>
                      )}
                    </div>
                  </td>
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
