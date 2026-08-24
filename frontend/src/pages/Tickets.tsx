import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { dataOf, http } from "../api/client";
import { Layout } from "../components/Layout";
import { formatLocal } from "../utils/format";

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

const STATUS_LABEL: Record<string, string> = {
  pending: "待处理",
  in_progress: "处理中",
  resolved: "已解决",
};

export default function Tickets() {
  const [items, setItems] = useState<TicketItem[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params: Record<string, string> =
        status === "all" ? {} : { status };
      const resp = await http.get("/tickets", { params });
      const data = dataOf<{ items: TicketItem[]; total: number }>(resp);
      setItems(data.items);
      setTotal(data.total);
    } catch {
      setError("加载工单失败");
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    load();
  }, [load]);

  const tabs = [
    { key: "all", label: "全部" },
    { key: "pending", label: "待处理" },
    { key: "in_progress", label: "处理中" },
    { key: "resolved", label: "已解决" },
  ];

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">工单 / SLA 待办</h1>
      <p className="text-sm text-gray-500 mb-4">
        高风险 24h 工单和人工回执的 2 个工作日工单统一在这里按 SLA 排序。
      </p>
      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}

      <div className="flex items-center bg-white border border-gray-300 rounded-md p-0.5 mb-4 w-fit">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setStatus(t.key)}
            className={`px-3 py-1 rounded text-sm leading-none ${
              status === t.key
                ? "bg-accent text-white font-medium"
                : "text-gray-500 hover:text-gray-800"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-gray-500">加载中…</p>
      ) : items.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-gray-500">
          暂无工单
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-4 py-2">摘要</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2">SLA 截止</th>
                <th className="px-4 py-2">等待</th>
                <th className="px-4 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className="border-t border-gray-100 hover:bg-blue-50">
                  <td className="px-4 py-2 max-w-md break-words">
                    {item.summary_cn}
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={
                        item.is_overdue
                          ? "px-2 py-0.5 rounded bg-red-50 text-red-600"
                          : "px-2 py-0.5 rounded bg-gray-100 text-gray-600"
                      }
                    >
                      {item.is_overdue ? "SLA 已逾期" : STATUS_LABEL[item.status] || item.status}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-gray-500">
                    {formatLocal(item.sla_deadline)}
                  </td>
                  <td className="px-4 py-2 text-gray-500">
                    {item.age_minutes >= 60
                      ? `${Math.floor(item.age_minutes / 60)}h`
                      : `${item.age_minutes}m`}
                  </td>
                  <td className="px-4 py-2">
                    <Link
                      to={`/conversations/${item.conversation_id}`}
                      className="text-accent hover:underline"
                    >
                      打开会话
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-4 py-3 border-t border-gray-200 text-sm text-gray-600">
            共 {total} 条
          </div>
        </div>
      )}
    </Layout>
  );
}
