import { useCallback, useEffect, useState } from "react";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";
import { formatLocal } from "../utils/format";

interface BlockedSender {
  id: number;
  value: string;
  scope: "email" | "domain";
  created_at: string | null;
}

export default function BlockedSenders() {
  const [items, setItems] = useState<BlockedSender[]>([]);
  const [value, setValue] = useState("");
  const [scope, setScope] = useState<"email" | "domain">("email");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const resp = await http.get("/blocked-senders");
      setItems(dataOf<{ items: BlockedSender[] }>(resp).items);
    } catch {
      setError("加载黑名单失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function add() {
    if (!value.trim()) {
      setError("请输入邮箱或域名");
      return;
    }
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await http.post("/blocked-senders", {
        value: value.trim(),
        scope,
      });
      setValue("");
      setSuccess("已加入黑名单");
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(item: BlockedSender) {
    if (!window.confirm(`确定解除「${item.value}」？`)) return;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await http.delete(`/blocked-senders/${item.id}`);
      setSuccess("已解除");
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">黑名单管理</h1>
      <p className="text-sm text-gray-500 mb-4">
        黑名单邮箱或域名会自动进入广告 tab，不再自动回复。
      </p>
      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}
      {success && <p className="text-sm text-green-600 mb-3">{success}</p>}

      <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4 flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">范围</label>
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as "email" | "domain")}
            className="border border-gray-300 rounded px-3 py-2 text-sm bg-white"
          >
            <option value="email">单个邮箱</option>
            <option value="domain">整个域名</option>
          </select>
        </div>
        <div className="flex-1 min-w-[240px]">
          <label className="block text-xs text-gray-500 mb-1">
            邮箱或域名
          </label>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={scope === "email" ? "user@example.com" : "example.com"}
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm"
          />
        </div>
        <button
          onClick={add}
          disabled={busy}
          className="px-4 py-2 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
        >
          加入黑名单
        </button>
      </div>

      {items.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-gray-500">
          暂无黑名单
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-4 py-2">值</th>
                <th className="px-4 py-2">范围</th>
                <th className="px-4 py-2">加入时间</th>
                <th className="px-4 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className="border-t border-gray-100 hover:bg-blue-50">
                  <td className="px-4 py-2 font-medium">{item.value}</td>
                  <td className="px-4 py-2 text-gray-600">
                    {item.scope === "email" ? "单个邮箱" : "整个域名"}
                  </td>
                  <td className="px-4 py-2 text-gray-500">
                    {formatLocal(item.created_at)}
                  </td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => remove(item)}
                      disabled={busy}
                      className="px-2 py-1 border border-red-300 text-red-600 rounded text-xs disabled:opacity-50"
                    >
                      解除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Layout>
  );
}
