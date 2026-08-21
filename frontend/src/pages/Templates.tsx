import { useCallback, useEffect, useState } from "react";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

interface ReplyTemplate {
  id: number;
  name: string;
  content: string;
  sort_order: number;
}

// Management page for the quick reply templates shown as one-click buttons in
// the conversation reply box. Add / edit / delete here propagates to the
// editor on its next load.
export default function Templates() {
  const [items, setItems] = useState<ReplyTemplate[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState(false);
  const [editing, setEditing] = useState<ReplyTemplate | null>(null);
  const [name, setName] = useState("");
  const [content, setContent] = useState("");

  const load = useCallback(async () => {
    try {
      const resp = await http.get("/reply-templates");
      setItems(dataOf<{ items: ReplyTemplate[] }>(resp).items);
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function openAdd() {
    setEditing(null);
    setName("");
    setContent("");
    setError("");
    setModal(true);
  }

  function openEdit(t: ReplyTemplate) {
    setEditing(t);
    setName(t.name);
    setContent(t.content);
    setError("");
    setModal(true);
  }

  async function save() {
    if (!name.trim() || !content.trim()) {
      setError("名称和内容都不能为空");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (editing) {
        await http.patch(`/reply-templates/${editing.id}`, {
          name: name.trim(),
          content: content.trim(),
        });
      } else {
        await http.post("/reply-templates", {
          name: name.trim(),
          content: content.trim(),
        });
      }
      setModal(false);
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(t: ReplyTemplate) {
    if (!window.confirm(`删除模板「${t.name}」？回复框里的按钮会同步消失。`)) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      await http.delete(`/reply-templates/${t.id}`);
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-[15px] font-semibold tracking-tight text-ink">
          快捷回复模板
          <span className="ml-2 font-normal text-sub">{items.length}</span>
        </h1>
        <button
          onClick={openAdd}
          className="px-3 py-1.5 rounded bg-accent text-white text-[13px] font-medium disabled:opacity-50"
          disabled={busy}
        >
          ＋ 新增模板
        </button>
      </div>

      {error && <p className="text-[13px] text-risk-high mb-3">{error}</p>}

      {items.length === 0 ? (
        <div className="bg-white rounded-lg border border-line p-10 text-center text-[13px] text-sub">
          还没有模板 — 点右上角「新增模板」添加。
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((t) => (
            <li
              key={t.id}
              className="bg-white rounded-lg border border-line p-4 flex items-start gap-3"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[14px] font-semibold text-ink">
                    {t.name}
                  </span>
                  <span className="text-[11px] text-sub">
                    回复框快捷按钮
                  </span>
                </div>
                <p className="mt-1 text-[13px] text-sub leading-relaxed whitespace-pre-wrap line-clamp-3">
                  {t.content}
                </p>
              </div>
              <div className="shrink-0 flex items-center gap-2">
                <button
                  onClick={() => openEdit(t)}
                  disabled={busy}
                  className="px-2.5 py-1 border border-line rounded text-[12px] text-sub hover:text-ink disabled:opacity-50"
                >
                  ✎ 编辑
                </button>
                <button
                  onClick={() => remove(t)}
                  disabled={busy}
                  className="px-2.5 py-1 border border-line rounded text-[12px] text-risk-high hover:bg-risk-high-tint disabled:opacity-50"
                >
                  🗑 删除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {modal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"
          onClick={() => !busy && setModal(false)}
        >
          <div
            className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-semibold text-ink mb-1">
              {editing ? "编辑模板" : "新增模板"}
            </h3>
            <p className="text-xs text-sub mb-3">
              快捷模板会出现在会话回复框里，点一下填入中文回复。
            </p>
            <label className="block text-[12px] text-sub mb-1">名称</label>
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              placeholder="如：退货 / 物流 / 发票"
              className="w-full border border-line rounded px-3 py-2 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-accent/30"
            />
            <label className="block text-[12px] text-sub mb-1">内容（中文）</label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={5}
              maxLength={2000}
              placeholder="输入给客户的预设中文回复…"
              className="w-full border border-line rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30"
            />
            {error && <p className="text-sm text-risk-high mt-2">{error}</p>}
            <div className="mt-3 flex gap-2 justify-end">
              <button
                onClick={() => setModal(false)}
                disabled={busy}
                className="px-3 py-1.5 border border-line rounded text-sm text-sub disabled:opacity-50"
              >
                取消
              </button>
              <button
                onClick={save}
                disabled={busy}
                className="px-3 py-1.5 bg-accent text-white rounded text-sm disabled:opacity-50"
              >
                {busy ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Layout>
  );
}
