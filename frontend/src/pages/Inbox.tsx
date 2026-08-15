import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { dataOf, http } from "../api/client";
import { Layout } from "../components/Layout";
import { RiskTag } from "../components/RiskTag";

interface InboxItem {
  id: number;
  conversation_id: number;
  subject: string;
  from_email: string;
  risk_level: string | null;
  summary_cn: string | null;
  received_at: string | null;
  status: string | null;
}

const STATUS_LABEL: Record<string, string> = {
  pending_review: "待审核",
  draft: "草稿",
  sent: "已发送",
  failed: "发送失败",
};

export default function Inbox() {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<"all" | "pending_review" | "high">("all");
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number> = { page, size: 20 };
      if (filter === "pending_review") params.status = "pending_review";
      if (filter === "high") params.risk_level = "high";
      if (keyword.trim()) params.keyword = keyword.trim();
      const resp = await http.get("/inbox", { params });
      const data = dataOf<{ items: InboxItem[]; total: number }>(resp);
      setItems(data.items);
      setTotal(data.total);
    } finally {
      setLoading(false);
    }
  }, [page, filter, keyword]);

  useEffect(() => {
    load();
  }, [load]);

  const tabs = [
    { key: "all" as const, label: "全部" },
    { key: "pending_review" as const, label: "待审核" },
    { key: "high" as const, label: "高风险" },
  ];

  return (
    <Layout>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-lg font-bold">收件箱</h1>
        <div className="flex gap-3">
          <input
            value={keyword}
            onChange={(e) => {
              setKeyword(e.target.value);
              setPage(1);
            }}
            placeholder="搜索主题 / 发件人 / 摘要"
            className="border border-gray-300 rounded px-3 py-1.5 text-sm w-64"
          />
        </div>
      </div>

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

      {loading ? (
        <p className="text-gray-500">加载中…</p>
      ) : items.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-gray-500">
          暂无邮件
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-4 py-2">风险</th>
                <th className="px-4 py-2">主题</th>
                <th className="px-4 py-2">发件人</th>
                <th className="px-4 py-2">中文摘要</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2">时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={item.id}
                  onClick={() =>
                    navigate(`/conversations/${item.conversation_id}`)
                  }
                  className="border-t border-gray-100 hover:bg-blue-50 cursor-pointer"
                >
                  <td className="px-4 py-2">
                    <RiskTag risk={item.risk_level} />
                  </td>
                  <td className="px-4 py-2 font-medium">{item.subject}</td>
                  <td className="px-4 py-2 text-gray-600">{item.from_email}</td>
                  <td className="px-4 py-2 text-gray-600 max-w-xs truncate">
                    {item.summary_cn || "—"}
                  </td>
                  <td className="px-4 py-2">
                    {item.status ? (
                      <span
                        className={`px-2 py-0.5 rounded text-xs ${
                          item.status === "pending_review"
                            ? "bg-blue-100 text-blue-700"
                            : item.status === "failed"
                              ? "bg-red-100 text-red-700"
                              : "bg-gray-100 text-gray-600"
                        }`}
                      >
                        {STATUS_LABEL[item.status] || item.status}
                      </span>
                    ) : (
                      "未回复"
                    )}
                  </td>
                  <td className="px-4 py-2 text-gray-500">
                    {item.received_at ? item.received_at.replace("T", " ").replace("Z", "") : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex items-center justify-between px-4 py-3 border-t border-gray-200 text-sm text-gray-600">
            <span>共 {total} 封</span>
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
