import { useCallback, useEffect, useState } from "react";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

interface QAPairItem {
  id: number;
  question: string;
  answer: string;
  category: string | null;
  enabled: boolean;
  updated_at: string | null;
}

export default function QAPairs() {
  const [items, setItems] = useState<QAPairItem[]>([]);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [category, setCategory] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const resp = await http.get("/qa-pairs");
      setItems(dataOf<{ items: QAPairItem[] }>(resp).items);
    } catch {
      setError("加载标准问答失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function resetForm() {
    setQuestion("");
    setAnswer("");
    setCategory("");
    setEditingId(null);
  }

  async function save() {
    if (!question.trim() || !answer.trim()) {
      setError("问题和答案都不能为空");
      return;
    }
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const payload = {
        question: question.trim(),
        answer: answer.trim(),
        category: category.trim() || null,
      };
      if (editingId === null) {
        await http.post("/qa-pairs", payload);
        setSuccess("已新增标准问答");
      } else {
        await http.patch(`/qa-pairs/${editingId}`, payload);
        setSuccess("已保存修改");
      }
      resetForm();
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  function startEdit(item: QAPairItem) {
    setEditingId(item.id);
    setQuestion(item.question);
    setAnswer(item.answer);
    setCategory(item.category || "");
    setError("");
    setSuccess("");
  }

  async function toggle(item: QAPairItem) {
    try {
      await http.patch(`/qa-pairs/${item.id}`, { enabled: !item.enabled });
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function remove(item: QAPairItem) {
    if (!window.confirm(`确定删除「${item.question}」？`)) return;
    try {
      await http.delete(`/qa-pairs/${item.id}`);
      setSuccess("已删除标准问答");
      if (editingId === item.id) resetForm();
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">标准问答（QA）</h1>
      <p className="text-sm text-gray-500 mb-4">
        老板维护的标准问答会在回复生成时全量注入；客户问题命中时直接输出标准英文答案（最多 100 条）。
      </p>

      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}
      {success && <p className="text-sm text-green-600 mb-3">{success}</p>}

      <div className="bg-white rounded-lg border border-gray-200 p-4 mb-6">
        <div className="grid gap-3">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="标准问题（英文，如 What is your return policy?）"
            className="px-3 py-2 border border-gray-300 rounded text-sm"
          />
          <textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder="标准答案（英文原文，命中时原样输出）"
            rows={3}
            className="px-3 py-2 border border-gray-300 rounded text-sm"
          />
          <div className="flex gap-2">
            <input
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              placeholder="分类标签（可选）"
              className="px-3 py-2 border border-gray-300 rounded text-sm flex-1"
            />
            <button
              onClick={save}
              disabled={busy}
              className="px-4 py-2 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
            >
              {editingId === null ? "新增" : "保存修改"}
            </button>
            {editingId !== null && (
              <button
                onClick={resetForm}
                className="px-4 py-2 border border-gray-300 rounded text-sm text-gray-600"
              >
                取消编辑
              </button>
            )}
          </div>
        </div>
      </div>

      {items.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-gray-500">
          暂无标准问答
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-4 py-2">问题</th>
                <th className="px-4 py-2">答案</th>
                <th className="px-4 py-2">分类</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className="border-t border-gray-100 hover:bg-blue-50 align-top">
                  <td className="px-4 py-2 max-w-xs break-words">{item.question}</td>
                  <td className="px-4 py-2 max-w-sm break-words text-gray-600">{item.answer}</td>
                  <td className="px-4 py-2 text-gray-500">{item.category || "—"}</td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => toggle(item)}
                      className={`px-2 py-1 rounded text-xs ${
                        item.enabled
                          ? "bg-green-100 text-green-700"
                          : "bg-gray-200 text-gray-600"
                      }`}
                    >
                      {item.enabled ? "已启用" : "已停用"}
                    </button>
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex gap-2">
                      <button
                        onClick={() => startEdit(item)}
                        className="px-2 py-1 border border-gray-300 rounded text-xs"
                      >
                        编辑
                      </button>
                      <button
                        onClick={() => remove(item)}
                        className="px-2 py-1 border border-red-300 text-red-600 rounded text-xs hover:bg-red-50"
                      >
                        删除
                      </button>
                    </div>
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
